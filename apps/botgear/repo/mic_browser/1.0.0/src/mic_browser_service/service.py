"""MicBrowserService — browser microphone, backend relay half.

Same ``microphone`` capability + publish shape as mic_local (inherited from
MicrophoneServiceBase), but the hardware lives in the operator's BROWSER:

  * CONTROL flows DOWN: connect/disconnect/mute/unmute/select are relayed
    to the browser as commands on ``/microphone/{id}/cmd``.
  * STATUS flows UP: the browser reports devices + connected/muted + level
    + a heartbeat on ``/microphone/{id}/report``, folded into the canonical
    ``/microphone/{id}/state``.
  * AUDIO flows UP: the browser publishes frames straight to
    ``/microphone/{id}/audio`` (the canonical topic). This service subscribes
    to that topic ONLY to feed save-to-file recording — the published shape
    is identical to mic_local's.

The ``cmd``/``report`` shapes are declared below (``MicCmd``,
``MicReport``) and introspected with the rest of the type.

A browser microphone only exists while an authorized tab is open; a stale
heartbeat marks ``last_error:"no browser client"`` and clears ``connected``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from rlx_audio import AudioFrame, MicrophoneServiceBase

logger = logging.getLogger(__name__)

_STALE_AFTER_S = 5.0
_CHECK_EVERY_S = 2.0


class MicCmd(BaseModel):
    """Published on ``/microphone/{id}/cmd`` — a command to the browser half."""
    action: Literal["enumerate", "connect", "disconnect", "set_muted"]
    device_id: Optional[str] = Field(None, description="connect")
    sample_rate: Optional[int] = Field(None, description="connect")
    channels: Optional[int] = Field(None, description="connect")
    frame_ms: Optional[int] = Field(None, description="connect")
    muted: Optional[bool] = Field(None, description="set_muted")


class MicReport(BaseModel):
    """Consumed from ``/microphone/{id}/report`` — browser status +
    heartbeat; a stale one (>5s) marks the mic disconnected."""
    model_config = ConfigDict(extra="allow")
    devices: Optional[List[Dict[str, Any]]] = None
    connected: Optional[bool] = None
    muted: Optional[bool] = None
    level_rms: Optional[float] = None
    error: Optional[str] = None
    ts: Optional[float] = None


class MicBrowserService(MicrophoneServiceBase):
    publishes = [*MicrophoneServiceBase.publishes, "cmd"]
    topic_schemas: Dict[str, Any] = {**MicrophoneServiceBase.topic_schemas, "cmd": MicCmd}
    subscribes_schemas = {"report": MicReport, "audio": AudioFrame}
    source = "browser"

    def __init__(self, proxy_id, bus) -> None:
        super().__init__(proxy_id, bus)
        self._last_report_ts: float = 0.0
        self._stale_task: Optional[asyncio.Task] = None

    async def on_start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.subscribe("report", self._on_report)
        # Tap the browser's audio frames for save-to-file recording.
        await self.subscribe("audio", self._record_from_frame)
        self._stale_task = asyncio.create_task(self._stale_loop())
        await super().on_start()

    async def on_stop(self) -> None:
        if self._stale_task is not None:
            self._stale_task.cancel()
        await super().on_stop()

    # ─── transport hooks — relay to the browser ───────────────────────
    async def _enumerate_devices(self):
        await self._cmd(MicCmd(action="enumerate"))
        return list(self._devices)

    async def _open(self) -> None:
        await self._cmd(MicCmd(
            action="connect",
            device_id=self.config.device_id,
            sample_rate=self.config.sample_rate,
            channels=self.config.channels,
            frame_ms=self.config.frame_ms,
        ))

    async def _close(self) -> None:
        await self._cmd(MicCmd(action="disconnect"))

    async def _on_mute_changed(self, muted: bool) -> None:
        await self._cmd(MicCmd(action="set_muted", muted=muted))

    async def _cmd(self, cmd: MicCmd) -> None:
        # exclude_unset (not exclude_none) so connect's explicit
        # device_id=None (= the browser's default device) still reaches
        # the browser, as before.
        await self.publish("cmd", cmd.model_dump(exclude_unset=True))

    # ─── browser → backend status ─────────────────────────────────────
    async def _on_report(self, payload: Any) -> None:
        """Fold a browser report into canonical state. Shape:
        ``{devices?, connected?, muted?, level_rms?, error?, ts}``."""
        if not isinstance(payload, dict):
            return
        self._last_report_ts = time.time()
        if isinstance(payload.get("devices"), list):
            self._devices = payload["devices"]
        if "connected" in payload:
            self._connected = bool(payload["connected"])
        if "level_rms" in payload:
            try:
                self._level = float(payload["level_rms"])
            except (TypeError, ValueError):
                pass
        self._last_error = payload.get("error") or None
        await self._publish_state()

    async def _stale_loop(self) -> None:
        while not self.is_stopping():
            await asyncio.sleep(_CHECK_EVERY_S)
            await self._check_stale()

    async def _check_stale(self) -> bool:
        if not self._last_report_ts:
            return False
        if (time.time() - self._last_report_ts) > _STALE_AFTER_S and self._connected:
            self._connected = False
            self._level = 0.0
            self._last_error = "no browser client"
            await self._publish_state()
            return True
        return False
