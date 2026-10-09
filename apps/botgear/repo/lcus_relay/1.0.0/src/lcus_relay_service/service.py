"""LcusRelayService — LCUS-1/2/4/8 USB relay module driver.

Subclasses ``rlx_bus.SubprocessService`` so the framework owns the
boilerplate (env, bus client, hello, control dispatch, heartbeat,
signals, graceful shutdown); this module defines only the LCUS-specific
config, the relay actions, and the serial/reconnect plumbing.

Wire shape is declared in code below (``LcusRelayState`` via
``state_schema``) and introspected into
``/runtime/runtime/types/lcus_relay``, as are this service's own
``control`` actions (off their @service_method signatures) — LCUS has
no promoted capability interface to implement, it's a leaf device.

Unlike a motor driver, relays are latching — there's no hardware
timeout failsafe and no host keepalive loop here. Once a channel is set
it stays set until commanded otherwise (or the board loses power), so
the state this service tracks is purely the last command it sent. A
best-effort ``query_status`` action re-syncs that tracked state against
the board's own report (0xFF query), for when something outside this
service changed a relay.

Subprocess service — pyserial does blocking I/O cleanly in its own
process, isolated from the backend event loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from rlx_bus import (
    ServiceConfig,
    SubprocessService,
    list_serial_ports,
    service_method,
)

from . import protocol
from .port_writer import SerialLink


logger = logging.getLogger(__name__)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── config ──────────────────────────────────────────────────────────


class LcusRelayConfig(ServiceConfig):
    """Strongly-typed config — every field survives a restart so the UI
    replays the operator's last selection. ``autoreconnect`` is the
    persisted *desired connection state*: it tracks whether the driver
    was connected, so a driver that was connected re-opens its port on
    the next start."""

    last_port: Optional[str] = Field(
        None,
        description="Device path of the last successful Connect (e.g. ``/dev/ttyUSB0``). UI pre-selects this next session.",
    )
    last_baud: int = Field(
        9600,
        description="Serial baud rate. LCUS boards default to 9600.",
    )
    channel_count: int = Field(
        8,
        description="Which LCUS variant is attached: LCUS-1 (1), LCUS-2 (2), LCUS-4 (4), or LCUS-8 (8). Only changes how many channels are exposed — the wire protocol is identical for all four.",
    )
    autoreconnect: bool = Field(
        False,
        description="Auto-connect: desired connection state. Set True automatically on a successful connect, False on an explicit disconnect. While True a background loop keeps retrying last_port whenever offline — on start, after a failed attempt, and after a live disconnect (e.g. USB unplug) is detected. Defaults False so a fresh instance never opens a guessed port.",
    )
    command_spacing_ms: int = Field(
        30, ge=0, le=1000,
        description="Minimum gap, in milliseconds, enforced between successive relay commands sent in the same burst (relay_all / a multi-channel sequence). A single relay_set/relay_toggle call is never delayed by this — it only paces back-to-back commands. Exists because pyserial's write() returns as soon as the OS accepts the bytes into its output buffer, not once they've actually gone out over the wire (at 9600 baud a 4-byte packet takes ~4ms to transmit) — cheap LCUS-clone firmware can drop or misparse a command that arrives while it's still busy actuating the previous relay. Raise this if All on/All off is unreliable; 0 disables pacing entirely (not recommended).",
    )


# ─── service ───────────────────────────────────────────────────────────


# ── wire schema for what this service PUBLISHES ───────────────────────

class LcusRelayState(BaseModel):
    """Retained on ``/lcus_relay/{id}/state`` — connection + the
    commanded on/off of every channel (last command sent; relays latch)."""
    connected: bool
    port: Optional[str]
    baudrate: int
    channel_count: int
    command_spacing_ms: int
    channels: List[int] = Field(description="Valid 1-based channel indices")
    relays: Dict[str, bool] = Field(description="str(channel) -> commanded on")
    ports: List[Dict[str, Any]] = Field(description="rlx_bus.list_serial_ports(): {device, description, hwid, holders, available}")
    last_port: Optional[str]
    last_baud: int
    autoreconnect: bool
    retrying: bool
    errors: int
    last_error: Optional[str]
    connected_at: Optional[str] = Field(description="ISO-8601 UTC, None when disconnected")


class LcusRelayService(SubprocessService):
    """LCUS-1/2/4/8 relay-board driver. See module docstring for the
    wire contract."""

    type_name = "lcus_relay"
    heartbeat_interval_s = 1.0
    config_class = LcusRelayConfig
    publishes = ["state"]
    state_schema = LcusRelayState

    # How often the auto-connect loop retries while offline / checks a
    # connected port node is still present.
    _AUTO_CONNECT_RETRY_S = 3.0

    def __init__(self, proxy_id, bus) -> None:
        super().__init__(proxy_id, bus)
        self._link: Optional[SerialLink] = None
        # Last commanded on/off per channel (1-based). Purely a local
        # model of what we've told the board — see module docstring.
        self._relays: Dict[int, bool] = {}
        self._errors: int = 0
        self._last_error: Optional[str] = None
        self._connected_at: Optional[str] = None
        self._auto_connect_task: Optional[asyncio.Task] = None
        self._reset_relays(self.config.channel_count)

    # ─── lifecycle ───────────────────────────────────────────────────
    async def on_start(self) -> None:
        await self._publish_state()
        if self.config.autoreconnect:
            self._start_auto_connect_loop()

    async def on_stop(self) -> None:
        self._stop_auto_connect_loop()
        await self._close()

    # ─── @service_method actions — connection ────────────────────────
    @service_method("list_ports")
    async def m_list_ports(self) -> Dict[str, Any]:
        """Rescan serial ports and republish state."""
        await self._publish_state()
        return {"ports": list_serial_ports()}

    @service_method("connect", publishes=["state"])
    async def m_connect(
        self,
        port: str,
        baudrate: Optional[int] = None,
        channel_count: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Open the port. Connect failures are survivable — the service
        keeps running so the operator can pick a different port. The
        error is surfaced on /state (``last_error``) and in the reply
        envelope, never raised as an opaque RPC failure."""
        if not port:
            raise ValueError("connect requires a port")
        baud = int(baudrate) if baudrate is not None else self.config.last_baud
        if channel_count is not None:
            await self._set_channel_count(channel_count)
        try:
            await self._open(port, baud)
        except Exception as exc:  # noqa: BLE001
            logger.exception("lcus_relay %s: connect to %s failed", self.proxy_id, port)
            self._errors += 1
            self._last_error = f"connect {port}: {exc}"
            await self._publish_state()
            return {"connected": False, "error": self._last_error}
        return self._snapshot()

    @service_method("disconnect", publishes=["state"])
    async def m_disconnect(self) -> Dict[str, Any]:
        """Close the port. ``last_port``/``last_baud`` stay in config so
        the UI re-selects them. Clears ``autoreconnect`` — an explicit
        disconnect means "stay down on the next start" (unlike a
        shutdown, which preserves it). Relay state on the board itself
        is untouched — disconnecting the controller doesn't flip relays."""
        self._stop_auto_connect_loop()
        await self._close()
        await self.update_config({"autoreconnect": False})
        return self._snapshot()

    @service_method("set_auto_connect", publishes=["state"])
    async def m_set_auto_connect(self, enabled: bool) -> Dict[str, Any]:
        """Toggle auto-connect independent of a manual connect/disconnect.
        When enabled and ``last_port`` is set, the driver keeps retrying a
        connect every few seconds whenever it's offline — including
        recovering on its own after the USB adapter is unplugged and
        plugged back in — until this is turned off or explicitly
        disconnected."""
        enabled = bool(enabled)
        await self.update_config({"autoreconnect": enabled})
        if enabled:
            self._start_auto_connect_loop()
        else:
            self._stop_auto_connect_loop()
        await self._publish_state()
        return self._snapshot()

    @service_method("set_command_spacing", publishes=["state"])
    async def m_set_command_spacing(self, command_spacing_ms: int) -> Dict[str, Any]:
        """Tune the inter-command gap used by relay_all (see
        command_spacing_ms on the config for why this exists). Raise it
        if All on/All off drops or misfires relays on your board."""
        ms = max(0, min(1000, int(command_spacing_ms)))
        await self.update_config({"command_spacing_ms": ms})
        await self._publish_state()
        return self._snapshot()

    @service_method("set_channel_count", publishes=["state"])
    async def m_set_channel_count(self, channel_count: int) -> Dict[str, Any]:
        """Switch which LCUS variant (1/2/4/8 channels) is attached.
        Safe to call whether connected or not — the wire protocol is
        identical for every variant, this only changes how many channels
        the UI/API expose."""
        await self._set_channel_count(channel_count)
        await self._publish_state()
        return self._snapshot()

    # ─── @service_method actions — relays ─────────────────────────────
    @service_method("relay_set", publishes=["state"])
    async def m_relay_set(self, channel: int, on: bool) -> Dict[str, Any]:
        """Set one relay channel on/off."""
        ch = self._require_channel(channel)
        on = bool(on)
        await self._send(protocol.relay_packet(ch, on))
        self._relays[ch] = on
        await self._publish_state()
        return {"channel": ch, "on": on}

    @service_method("relay_toggle", publishes=["state"])
    async def m_relay_toggle(self, channel: int) -> Dict[str, Any]:
        """Flip one relay channel from its last-known (tracked) state."""
        ch = self._require_channel(channel)
        new_state = not self._relays.get(ch, False)
        await self._send(protocol.relay_packet(ch, new_state))
        self._relays[ch] = new_state
        await self._publish_state()
        return {"channel": ch, "on": new_state}

    @service_method("relay_all", publishes=["state"])
    async def m_relay_all(self, on: bool) -> Dict[str, Any]:
        """Set every channel on/off. Best-effort: each channel is sent
        independently so one failing write doesn't leave the rest
        unset.

        Paced by ``command_spacing_ms`` between channels: pyserial's
        write() returns as soon as the OS has *queued* the bytes, not
        once they've actually gone out over the wire, so firing all N
        packets back-to-back can land them at the board almost
        simultaneously — a real burst-buffer-overrun risk on cheap
        LCUS-clone firmware, not just an artifact of this UI button.
        Each write is followed by a hardware flush (blocks until the
        bytes are actually transmitted) plus the configured settle gap,
        so the board has time to finish actuating one relay before the
        next command arrives."""
        on = bool(on)
        channels = protocol.channel_range(self.config.channel_count)
        spacing_s = max(0, self.config.command_spacing_ms) / 1000.0
        for i, ch in enumerate(channels):
            try:
                await self._send(protocol.relay_packet(ch, on), flush=True)
                self._relays[ch] = on
            except Exception:  # noqa: BLE001
                logger.exception("lcus_relay %s: relay_all channel %d failed", self.proxy_id, ch)
            if spacing_s > 0 and i < len(channels) - 1:
                await asyncio.sleep(spacing_s)
        await self._publish_state()
        return {"on": on}

    @service_method("query_status", publishes=["state"])
    async def m_query_status(self) -> Dict[str, Any]:
        """Best-effort hardware read-back: send the 0xFF status query
        and re-sync tracked state from the reply. Not every clone board
        implements this — a short/empty reply is reported as an error
        but doesn't disturb the previously-tracked state."""
        if self._link is None:
            raise RuntimeError("lcus_relay is not connected — call connect() first")
        n = self.config.channel_count
        try:
            reply = await self._link.write_read(protocol.query_packet(), n, timeout=0.5)
            status = protocol.parse_status(reply, n)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"query_status: {exc}"
            await self._publish_state()
            return {"ok": False, "error": self._last_error}
        self._relays.update(status)
        await self._publish_state()
        return {"ok": True, "relays": {str(k): v for k, v in status.items()}}

    # ─── internals ───────────────────────────────────────────────────
    def _reset_relays(self, channel_count: int) -> None:
        self._relays = {ch: False for ch in protocol.channel_range(channel_count)}

    async def _set_channel_count(self, channel_count: int) -> None:
        count = int(channel_count)
        if count not in protocol.VALID_CHANNEL_COUNTS:
            raise ValueError(
                f"channel_count must be one of {protocol.VALID_CHANNEL_COUNTS} (got {channel_count})"
            )
        await self.update_config({"channel_count": count})
        # Keep any still-valid channels' tracked state, drop the rest,
        # and add freshly-exposed channels as "off" (unknown really, but
        # off is the safer assumption to render).
        self._relays = {
            ch: self._relays.get(ch, False) for ch in protocol.channel_range(count)
        }

    def _require_channel(self, channel: Any) -> int:
        ch = int(channel)
        valid = protocol.channel_range(self.config.channel_count)
        if ch not in valid:
            raise ValueError(f"channel must be one of {valid} (got {channel})")
        return ch

    async def _send(self, data: bytes, *, flush: bool = False) -> None:
        """Write a packet to the link. Raises if not connected so the
        caller's reply envelope carries the failure. ``flush=True``
        additionally blocks until the bytes have actually left the host
        (not just been queued by the OS) — used when sending several
        packets in a row (relay_all) so the next packet isn't queued
        before this one has physically gone out over the wire."""
        if self._link is None:
            raise RuntimeError("lcus_relay is not connected — call connect() first")
        try:
            await self._link.write(data)
            if flush:
                await self._link.flush()
        except Exception as exc:  # noqa: BLE001
            self._errors += 1
            self._last_error = f"write: {exc}"
            await self._publish_state()
            raise

    async def _open(self, port: str, baudrate: int) -> None:
        """Open the port and try a best-effort status query to sync
        tracked relay state with reality (e.g. after a backend restart
        while the board stayed powered)."""
        await self._close()
        link = SerialLink(port=port, baudrate=int(baudrate), logger=logger)
        await link.open()
        self._link = link
        self._connected_at = _iso_now()
        self._last_error = None
        try:
            n = self.config.channel_count
            reply = await link.write_read(protocol.query_packet(), n, timeout=0.5)
            status = protocol.parse_status(reply, n)
            self._relays.update(status)
        except Exception:  # noqa: BLE001
            # Best-effort only — plenty of clone boards don't answer
            # the query. Fall back to "everything tracked as off".
            self._reset_relays(self.config.channel_count)
        await self.update_config({"last_port": port, "last_baud": int(baudrate), "autoreconnect": True})
        self._start_auto_connect_loop()
        await self._publish_state()

    async def _close(self) -> None:
        """Close the link. Idempotent. Does not touch relay state on the
        board — only the controller connection is torn down."""
        if self._link is not None:
            await self._link.close()
            self._link = None
        self._connected_at = None
        await self._publish_state()

    # ─── auto-connect ──────────────────────────────────────────────
    def _start_auto_connect_loop(self) -> None:
        """(Re)arm the background retry loop. Idempotent."""
        if self._auto_connect_task is not None and not self._auto_connect_task.done():
            return
        self._auto_connect_task = asyncio.create_task(self._auto_connect_loop())

    def _stop_auto_connect_loop(self) -> None:
        if self._auto_connect_task is not None and not self._auto_connect_task.done():
            self._auto_connect_task.cancel()
        self._auto_connect_task = None

    async def _auto_connect_loop(self) -> None:
        """Runs for as long as ``autoreconnect`` stays True: connects to
        ``last_port`` whenever offline (retrying every
        ``_AUTO_CONNECT_RETRY_S`` on failure), and — while connected —
        periodically checks the port node is still present so an unplug
        is noticed even though relay commands are otherwise idle."""
        try:
            while self.config.autoreconnect:
                if self._link is None:
                    if self.config.last_port:
                        try:
                            await self._open(self.config.last_port, self.config.last_baud)
                        except Exception as exc:  # noqa: BLE001
                            self._errors += 1
                            self._last_error = f"auto-connect {self.config.last_port}: {exc}"
                            await self._publish_state()
                elif not await asyncio.to_thread(os.path.exists, self._link.port):
                    await self._handle_link_lost(f"auto-connect: {self._link.port} no longer present")
                await asyncio.sleep(self._AUTO_CONNECT_RETRY_S)
        except asyncio.CancelledError:
            pass

    async def _handle_link_lost(self, reason: str) -> None:
        """Tear down a link that died out from under us (vanished port
        node) so ``/state`` reflects reality immediately and the
        auto-connect loop's next tick retries."""
        self._errors += 1
        self._last_error = reason
        await self._close()

    # ─── state ─────────────────────────────────────────────────────
    def _snapshot(self) -> Dict[str, Any]:
        connected = self._link is not None
        state = LcusRelayState(
            connected=connected,
            port=self._link.port if connected else None,
            baudrate=self._link.baudrate if connected else self.config.last_baud,
            channel_count=self.config.channel_count,
            command_spacing_ms=self.config.command_spacing_ms,
            channels=protocol.channel_range(self.config.channel_count),
            relays={str(ch): self._relays.get(ch, False) for ch in protocol.channel_range(self.config.channel_count)},
            ports=list_serial_ports(),
            last_port=self.config.last_port,
            last_baud=self.config.last_baud,
            autoreconnect=self.config.autoreconnect,
            retrying=self.config.autoreconnect and not connected,
            errors=self._errors,
            last_error=self._last_error,
            connected_at=self._connected_at,
        )
        return state.model_dump()

    async def _publish_state(self) -> None:
        await self.publish("state", self._snapshot(), retained=True)
