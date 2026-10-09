"""JoystickService — cross-platform joystick / gamepad reader.

A subprocess service backed by pygame (SDL2). It enumerates the
host's joystick devices, attaches to one, and streams its live control
values — axes (analog), buttons (digital), hats (d-pads), and balls
(trackballs) — over the bus. Different controllers expose wildly
different component layouts, so the wire shape is fully dynamic: the
service publishes how many of each component the attached device has,
and the UI builds itself from that.

Subprocess service — SDL's event pump + blocking device reads live on
their own thread in their own process, isolated from the backend.

Wire shape is declared in code below (``JoystickState``,
``InputPayload`` via ``state_schema``/``topic_schemas``) and
introspected into ``/runtime/runtime/types/joystick``; ``control``
actions are discoverable the same way, off their @service_method
signatures.

``state`` and ``input`` are deliberately split: ``state`` is the
layout (republished rarely), ``input`` is the values (republished at
the poll rate, but only on change). The UI subscribes to both.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from rlx_bus import ServiceConfig, SubprocessService, service_method

from .poller import Poller


logger = logging.getLogger(__name__)


# ─── config ──────────────────────────────────────────────────────────


class JoystickConfig(ServiceConfig):
    """Strongly-typed config — survives restarts so the UI replays the
    operator's last device + params. ``autoreconnect`` is the persisted
    *desired connection state*: it tracks whether a device was attached,
    so a controller that was attached is re-attached on the next start
    (and one explicitly detached stays detached)."""

    last_index: Optional[int] = Field(
        None, ge=0,
        description="Index of the last attached device. UI pre-selects it next session.",
    )
    last_guid: Optional[str] = Field(
        None,
        description="SDL GUID of the last attached device — stable across reconnects, unlike the index. Used to find the same controller again when autoreconnect is on.",
    )
    enabled: bool = Field(
        True,
        description="When False the device stays attached but value polling/streaming pauses (the UI's enable toggle).",
    )
    poll_hz: int = Field(
        60, ge=1, le=250,
        description="Value poll rate in Hz. 60 matches typical controller report rates; higher just burns CPU.",
    )
    deadzone: float = Field(
        0.05, ge=0.0, le=1.0,
        description="Axis deadzone — values within this magnitude of center read as 0, killing resting-stick jitter on the bus.",
    )
    autoreconnect: bool = Field(
        False,
        description="Auto-connect: desired connection state. Set True automatically when a device is attached, False on an explicit detach (toggle directly with set_auto_connect). While True the driver keeps a background loop alive that attaches the last device (matched by GUID — stable across replugs, unlike index) whenever detached — on start, and after an unplug is detected — retrying every few seconds until it succeeds or this is turned off. Defaults False so a fresh instance never grabs a guessed controller.",
    )


# ─── service ───────────────────────────────────────────────────────────


# ── wire schemas for what this service PUBLISHES ──────────────────────

class DeviceInfo(BaseModel):
    """One enumerated joystick (see ``poller._meta``)."""
    index: int
    name: str
    guid: str
    num_axes: int
    num_buttons: int
    num_hats: int
    num_balls: int


class ComponentCounts(BaseModel):
    axes: int
    buttons: int
    hats: int
    balls: int


class JoystickState(BaseModel):
    """Retained on ``/joystick/{id}/state`` — the slow-changing layout:
    devices, attached device + component counts, params, last_error."""
    attached: bool
    enabled: bool
    devices: List[DeviceInfo]
    device: Optional[DeviceInfo]
    components: ComponentCounts
    poll_hz: int
    deadzone: float
    last_index: Optional[int]
    autoreconnect: bool
    retrying: bool = Field(description="autoreconnect is on but no device is attached yet")
    last_error: Optional[str]


class InputPayload(BaseModel):
    """Published (non-retained) on ``/joystick/{id}/input`` — the live
    values, only when something changes (deadzone-filtered), so an idle
    stick is silent on the bus."""
    axes: List[float] = Field(description="-1..+1 per axis, deadzone-quantized")
    buttons: List[int] = Field(description="0/1 per button")
    hats: List[List[int]] = Field(description="[x, y] per hat, each -1/0/1")
    balls: List[List[int]] = Field(description="[dx, dy] relative motion per trackball")
    ts: float


class JoystickService(SubprocessService):
    """Cross-platform joystick reader. See module docstring for the
    full wire contract."""

    type_name = "joystick"
    heartbeat_interval_s = 1.0
    config_class = JoystickConfig
    publishes = ["state", "input"]
    state_schema = JoystickState
    topic_schemas = {"input": InputPayload}

    # How often the auto-connect loop retries a reattach while detached.
    # A few seconds is enough to feel prompt on a replug without spamming
    # attach attempts for a controller that's genuinely gone.
    _AUTO_CONNECT_RETRY_S = 3.0

    def __init__(self, proxy_id, bus) -> None:
        super().__init__(proxy_id, bus)
        self._poller: Optional[Poller] = None
        self._devices: List[Dict[str, Any]] = []
        self._device: Optional[Dict[str, Any]] = None     # attached device meta
        self._last_input: Optional[Dict[str, Any]] = None
        self._last_error: Optional[str] = None
        self._auto_connect_task: Optional[asyncio.Task] = None

    # ─── lifecycle ───────────────────────────────────────────────────
    async def on_start(self) -> None:
        self._ensure_poller()
        await self._publish_state()
        if self.config.autoreconnect:
            self._start_auto_connect_loop()

    async def on_stop(self) -> None:
        self._stop_auto_connect_loop()
        if self._poller is not None:
            await self._poller.stop()
            self._poller = None

    def _ensure_poller(self) -> Poller:
        """Create + start the poll thread once. Enumeration begins
        immediately so the device dropdown populates before any attach."""
        if self._poller is None:
            self._poller = Poller(
                on_devices=self._on_devices,
                on_attached=self._on_attached,
                on_input=self._on_input,
                on_error=self._on_error,
                poll_hz=self.config.poll_hz,
                deadzone=self.config.deadzone,
                enabled=self.config.enabled,
                logger=logger,
            )
            self._poller.start()
        return self._poller

    # ─── poller callbacks (run on the asyncio loop) ──────────────────
    async def _on_devices(self, devices: List[Dict[str, Any]]) -> None:
        self._devices = devices
        await self._publish_state()

    async def _on_attached(self, meta: Optional[Dict[str, Any]]) -> None:
        self._device = meta
        if meta is None:
            self._last_input = None
        else:
            self._last_error = None
            # Persist the device + the desired connection state so this
            # controller is re-attached on the next start.
            await self.update_config({
                "last_index": int(meta.get("index", 0)),
                "last_guid": meta.get("guid"),
                "autoreconnect": True,
            })
            self._start_auto_connect_loop()
        await self._publish_state()

    async def _on_input(self, snap: Dict[str, Any]) -> None:
        self._last_input = snap
        try:
            await self.publish("input", InputPayload(**snap).model_dump(), retained=False)
        except Exception:  # noqa: BLE001
            logger.exception("joystick %s: input publish failed", self.proxy_id)

    async def _on_error(self, message: str) -> None:
        self._last_error = message
        await self._publish_state()

    # ─── @service_method actions ─────────────────────────────────────
    @service_method("list_devices")
    async def m_list_devices(self) -> Dict[str, Any]:
        """Rescan + republish. Returns the current device list so a
        synchronous caller doesn't have to also subscribe to /state."""
        self._ensure_poller().rescan()
        return {"devices": self._devices}

    @service_method("attach", publishes=["state", "input"])
    async def m_attach(self, index: int) -> Dict[str, Any]:
        """Open device ``index`` and start streaming its values. The
        attached metadata + component counts arrive on /state shortly
        after via the poller callback."""
        self._ensure_poller().attach(int(index))
        return self._snapshot()

    @service_method("detach", publishes=["state"])
    async def m_detach(self) -> Dict[str, Any]:
        """Release the attached device. The device list keeps updating.
        Clears ``autoreconnect`` — an explicit detach means "stay
        detached on the next start". (An unexpected device drop does NOT
        clear it, so a controller that unplugs/replugs re-attaches.)"""
        self._stop_auto_connect_loop()
        await self.update_config({"autoreconnect": False})
        if self._poller is not None:
            self._poller.detach()
        return self._snapshot()

    @service_method("set_auto_connect", publishes=["state"])
    async def m_set_auto_connect(self, enabled: bool) -> Dict[str, Any]:
        """Toggle auto-connect independent of a manual attach/detach.
        When enabled, the driver keeps retrying an attach to the last
        device (matched by GUID) every few seconds whenever it's
        detached — including recovering on its own after the controller
        is unplugged and plugged back in — until this is turned off or
        the driver is explicitly detached."""
        enabled = bool(enabled)
        await self.update_config({"autoreconnect": enabled})
        if enabled:
            self._start_auto_connect_loop()
        else:
            self._stop_auto_connect_loop()
        await self._publish_state()
        return self._snapshot()

    @service_method("set_enabled", publishes=["state"])
    async def m_set_enabled(self, enabled: bool) -> Dict[str, Any]:
        """Pause/resume value streaming without detaching."""
        flag = bool(enabled)
        await self.update_config({"enabled": flag})
        if self._poller is not None:
            self._poller.set_enabled(flag)
        await self._publish_state()
        return {"enabled": flag}

    @service_method("set_params", publishes=["state"])
    async def m_set_params(
        self,
        poll_hz: Optional[int] = None,
        deadzone: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Live-tune the poll rate and/or axis deadzone."""
        updates: Dict[str, Any] = {}
        if poll_hz is not None:
            updates["poll_hz"] = max(1, min(250, int(poll_hz)))
        if deadzone is not None:
            updates["deadzone"] = max(0.0, min(1.0, float(deadzone)))
        if updates:
            await self.update_config(updates)
        if self._poller is not None:
            self._poller.set_params(updates.get("poll_hz"), updates.get("deadzone"))
        await self._publish_state()
        return self._snapshot()

    # ─── auto-connect ──────────────────────────────────────────────
    def _start_auto_connect_loop(self) -> None:
        """(Re)arm the background retry loop. Idempotent — a second
        call while one is already running is a no-op."""
        if self._auto_connect_task is not None and not self._auto_connect_task.done():
            return
        self._auto_connect_task = asyncio.create_task(self._auto_connect_loop())

    def _stop_auto_connect_loop(self) -> None:
        if self._auto_connect_task is not None and not self._auto_connect_task.done():
            self._auto_connect_task.cancel()
        self._auto_connect_task = None

    def _find_reconnect_target(self) -> Optional[int]:
        """Resolve which device index to (re)attach to for auto-connect.
        Prefers a GUID match against the current device list — stable
        across replugs, where the OS may hand a reconnected controller a
        different index than it had before — falling back to the last
        known index only if no GUID was ever recorded (an older config).
        Returns None (wait for the next tick) if the target device isn't
        currently present."""
        guid = self.config.last_guid
        if guid:
            for d in self._devices:
                if d.get("guid") == guid:
                    return int(d["index"])
            return None
        if self.config.last_index is not None:
            return int(self.config.last_index)
        return None

    async def _auto_connect_loop(self) -> None:
        """Runs for as long as ``autoreconnect`` stays True: attaches the
        last device whenever detached (retrying every
        ``_AUTO_CONNECT_RETRY_S`` until the GUID match reappears). The
        poller's own hotplug scan already detects a live disconnect and
        clears ``self._device`` — this loop only needs to notice that and
        try again."""
        try:
            while self.config.autoreconnect:
                if self._device is None:
                    target = self._find_reconnect_target()
                    if target is not None:
                        self._ensure_poller().attach(target)
                await asyncio.sleep(self._AUTO_CONNECT_RETRY_S)
        except asyncio.CancelledError:
            pass

    # ─── state ─────────────────────────────────────────────────────
    def _components(self) -> Dict[str, int]:
        d = self._device or {}
        return {
            "axes": int(d.get("num_axes", 0)),
            "buttons": int(d.get("num_buttons", 0)),
            "hats": int(d.get("num_hats", 0)),
            "balls": int(d.get("num_balls", 0)),
        }

    def _snapshot(self) -> Dict[str, Any]:
        state = JoystickState(
            attached=self._device is not None,
            enabled=self.config.enabled,
            devices=self._devices,
            device=self._device,
            components=self._components(),
            poll_hz=self.config.poll_hz,
            deadzone=self.config.deadzone,
            last_index=self.config.last_index,
            autoreconnect=self.config.autoreconnect,
            retrying=self.config.autoreconnect and self._device is None,
            last_error=self._last_error,
        )
        return state.model_dump()

    async def _publish_state(self) -> None:
        await self.publish("state", self._snapshot(), retained=True)
