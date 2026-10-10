"""TasmotaService — botgear Tasmota HTTP relay-switch subprocess service.

Implements the ``power_switch`` capability interface (see AGENTS.md's
"Promoted interfaces today" table) against any device running Tasmota
firmware. See ``tasmota_client.py`` for why targeting the firmware
(rather than "Sonoff", the product the user happens to own) means this
one service needs no per-product code.

Subprocess service so a slow/unreachable device's HTTP timeouts can
never block the backend event loop — same reasoning as every other
hardware service here (arduino, video, ...).
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field
from rlx_bus import ServiceConfig, SubprocessService, service_method

from .tasmota_client import TasmotaClient, parse_relays, scan_subnet


logger = logging.getLogger(__name__)


class TasmotaConfig(ServiceConfig):
    """Fields:
      * host — device IP/hostname. ``None`` until Connect (manual or
        from a scan result) — the poll loop idles rather than guessing.
      * username/password — Tasmota's optional web-admin auth, sent as
        ``user=``/``password=`` query params. Devices without a web
        password set (the vast majority out of the box) leave these
        unset.
      * poll_interval_s — how often the status loop re-polls a
        connected device.
      * timeout_s — per-HTTP-request timeout, connect + poll alike.
    """
    host: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    poll_interval_s: float = 5.0
    timeout_s: float = 3.0


# Consecutive failed polls before a connected device flips to
# reachable=False on /state. Debounces a single dropped packet or a
# brief power-cycle rather than flapping the UI red on every hiccup.
_UNREACHABLE_AFTER = 2


# ── wire schemas for what this service PUBLISHES ──────────────────────

class Relay(BaseModel):
    index: int = Field(description="1-based relay number")
    on: bool
    label: str


class TasmotaState(BaseModel):
    """Retained on ``/tasmota/{id}/state`` — the power_switch state."""
    host: Optional[str]
    reachable: bool
    last_seen: Optional[float]
    friendly_name: Optional[str]
    module: Optional[Any] = Field(description="Tasmota's Status.Module")
    relays: List[Relay]


class SensorPayload(BaseModel):
    """Retained on ``/tasmota/{id}/sensor`` — the device's raw
    ``StatusSNS`` passed through verbatim (energy monitors, temperature
    sensors, ...), only when it reports one. No per-sensor parsing, so a
    new device type "just works" for telemetry."""
    model_config = ConfigDict(extra="allow")


class TasmotaService(SubprocessService):
    """Implements ``power_switch``. Wire shape is declared by the models
    above (``state_schema``/``topic_schemas``)."""

    type_name = "tasmota"
    heartbeat_interval_s = 1.0
    config_class = TasmotaConfig
    publishes = ["state", "sensor"]
    state_schema = TasmotaState
    topic_schemas = {"sensor": SensorPayload}

    def __init__(self, proxy_id, bus) -> None:
        super().__init__(proxy_id, bus)
        self._relays: List[Dict[str, Any]] = []
        self._reachable = False
        self._consecutive_fails = 0
        self._last_seen: Optional[float] = None
        self._friendly_name: Optional[str] = None
        self._module: Optional[str] = None
        self._poll_task: Optional[asyncio.Task] = None

    # ─── lifecycle ───────────────────────────────────────────────────
    async def on_start(self) -> None:
        await self.publish_state()
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def on_stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def _client(self) -> Optional[TasmotaClient]:
        if not self.config.host:
            return None
        return TasmotaClient(
            self.config.host, username=self.config.username,
            password=self.config.password, timeout_s=self.config.timeout_s,
        )

    # ─── poll loop ───────────────────────────────────────────────────
    # No "give up after N reopens" ceremony here (unlike video's camera
    # wedge handling) — an HTTP GET has no device handle to leak or
    # wedge, every poll is an independent attempt, and a switch that's
    # unreachable now (Wi-Fi hiccup, power-cycling) is still worth
    # retrying indefinitely rather than forcing a manual reconnect.
    async def _poll_loop(self) -> None:
        try:
            while not self.is_stopping():
                if self.config.host:
                    await self._poll_once()
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self.config.poll_interval_s,
                    )
                    return
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            return

    async def _poll_once(self) -> None:
        client = self._client()
        if client is None:
            return
        try:
            payload = await client.status()
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s: status poll failed: %s", self.proxy_id, exc)
            self._consecutive_fails += 1
            if self._consecutive_fails >= _UNREACHABLE_AFTER and self._reachable:
                self._reachable = False
                await self.publish_state()
            return
        self._consecutive_fails = 0
        self._reachable = True
        self._last_seen = time.time()
        self._apply_status(payload)
        await self.publish_state()
        sns = payload.get("StatusSNS")
        if sns:
            await self.publish("sensor", SensorPayload(**sns).model_dump(), retained=True)

    def _apply_status(self, payload: Dict[str, Any]) -> None:
        status = payload.get("Status") or {}
        sts = payload.get("StatusSTS") or {}
        friendly = status.get("FriendlyName") or []
        if friendly:
            self._friendly_name = friendly[0]
        if "Module" in status:
            self._module = status.get("Module")
        if sts:
            self._relays = parse_relays(sts, friendly)

    # ─── state ───────────────────────────────────────────────────────
    def _snapshot(self) -> Dict[str, Any]:
        state = TasmotaState(
            host=self.config.host,
            reachable=self._reachable,
            last_seen=self._last_seen,
            friendly_name=self._friendly_name,
            module=self._module,
            relays=self._relays,
        )
        return state.model_dump()

    async def publish_state(self) -> None:
        await self.publish("state", self._snapshot(), retained=True)

    def _find_relay(self, relay: int) -> Optional[Dict[str, Any]]:
        for r in self._relays:
            if r["index"] == relay:
                return r
        return None

    def _set_relay_optimistic(self, relay: int, on: bool) -> None:
        """Reflect a just-issued command in ``/state`` immediately rather
        than waiting for the next poll tick — the device did the thing,
        no reason to make the UI wait ``poll_interval_s`` to find out."""
        r = self._find_relay(relay)
        if r is not None:
            r["on"] = on
        else:
            self._relays.append({"index": relay, "on": on, "label": f"Relay {relay}"})
            self._relays.sort(key=lambda x: x["index"])

    # ─── power_switch capability ───────────────────────────────────────
    # args_schema is hand-written rather than introspected from the
    # signature: it adds `additionalProperties: false`, which blocks the
    # phantom arguments a brain model was observed inventing (and a
    # guessed `relay` default) back when subprocess methods carried no
    # schema at all.
    @service_method("power_set", publishes=["state"], args_schema={
        "type": "object",
        "properties": {
            "relay": {"type": "integer", "default": 1,
                       "description": "Which relay/output to address (default: the first/only one)."},
            "on": {"type": "boolean", "default": True,
                   "description": "True to switch on, False to switch off."},
        },
        "additionalProperties": False,
    })
    async def m_power_set(self, relay: int = 1, on: bool = True) -> Dict[str, Any]:
        """Turn this device's relay on or off explicitly — pass
        ``on=True`` to switch on, ``on=False`` to switch off. ``relay``
        selects which output on a multi-relay device (default: the
        first/only one)."""
        client = self._client()
        if client is None:
            return {"error": "no host configured — call connect first"}
        await client.set_power(int(relay), bool(on))
        self._set_relay_optimistic(int(relay), bool(on))
        await self.publish_state()
        return {"relay": int(relay), "on": bool(on)}

    @service_method("power_toggle", publishes=["state"], args_schema={
        "type": "object",
        "properties": {
            "relay": {"type": "integer", "default": 1,
                       "description": "Which relay/output to address (default: the first/only one)."},
        },
        "additionalProperties": False,
    })
    async def m_power_toggle(self, relay: int = 1) -> Dict[str, Any]:
        """Flip this device's relay to the opposite of its current
        state. Use ``power_set`` instead when the desired end state
        (on vs. off) is known — toggling blind risks doing the wrong
        thing if the current state was already what was wanted."""
        client = self._client()
        if client is None:
            return {"error": "no host configured — call connect first"}
        await client.toggle_power(int(relay))
        current = self._find_relay(int(relay))
        new_on = (not current["on"]) if current is not None else True
        self._set_relay_optimistic(int(relay), new_on)
        await self.publish_state()
        return {"relay": int(relay), "on": new_on}

    @service_method("pulse", publishes=["state"])
    async def m_pulse(self, relay: int = 1, ms: int = 1000) -> Dict[str, Any]:
        """Momentary ON via the DEVICE's own PulseTime auto-off timer —
        hardware-guaranteed cutoff even if botgear crashes mid-pulse.
        Useful for garage-door-style actuators wired through a relay."""
        client = self._client()
        if client is None:
            return {"error": "no host configured — call connect first"}
        await client.pulse(int(relay), int(ms))
        self._set_relay_optimistic(int(relay), True)
        await self.publish_state()
        return {"relay": int(relay), "ms": int(ms)}

    @service_method("list_relays")
    async def m_list_relays(self) -> Dict[str, Any]:
        """No network round trip — the poll loop keeps this cache fresh,
        and every mutating action above updates it optimistically."""
        return {"relays": [dict(r) for r in self._relays]}

    # ─── connection management ─────────────────────────────────────────
    @service_method("connect", publishes=["state"])
    async def m_connect(
        self, host: str, username: Optional[str] = None, password: Optional[str] = None,
    ) -> Dict[str, Any]:
        await self.update_config({"host": host, "username": username, "password": password})
        self._consecutive_fails = 0
        await self._poll_once()
        if not self._reachable:
            return {"error": f"could not reach {host}"}
        return self._snapshot()

    @service_method("disconnect", publishes=["state"])
    async def m_disconnect(self) -> Dict[str, Any]:
        await self.update_config({"host": None})
        self._relays = []
        self._reachable = False
        self._last_seen = None
        await self.publish_state()
        return {"disconnected": True}

    @service_method("scan")
    async def m_scan(self, subnet: Optional[str] = None, timeout_s: float = 1.0) -> Dict[str, Any]:
        """Best-effort LAN sweep for unauthenticated Tasmota devices —
        see ``scan_subnet``'s docstring for why this exists instead of
        mDNS. Doesn't auto-connect; returns candidates for the UI to
        present."""
        prefix = subnet or self._local_subnet_prefix()
        if prefix is None:
            return {"candidates": [], "error": "could not determine local subnet"}
        candidates = await scan_subnet(prefix, timeout_s=float(timeout_s))
        return {"candidates": candidates, "subnet": f"{prefix}.0/24"}

    @staticmethod
    def _local_subnet_prefix() -> Optional[str]:
        """Best-effort local /24 prefix with no extra dependency: a UDP
        'connect' (no packet actually sent for UDP) just asks the kernel
        which local address would be used to reach the internet, then
        assumes that address's /24. Deliberately dependency-free (unlike
        the backend's own ``ifaddr``-based interface enumeration) since
        this subprocess venv has no reason to carry that extra package
        for one best-effort guess."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
        except OSError:
            return None
        return ".".join(local_ip.split(".")[:3])
