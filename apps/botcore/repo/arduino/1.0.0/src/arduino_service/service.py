"""ArduinoService — botcore arduino subprocess service.

Subclasses ``rlx_bus.SubprocessService`` so the boilerplate (env loading,
bus client, hello announcement, control dispatch, heartbeat, signals,
consume loop, graceful shutdown) lives in the framework. This module
defines only what's arduino-specific: the @service_method actions, the
state publish, and the pymata_wrapper integration.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

from rlx_bus import (
    ServiceConfig,
    SubprocessService,
    list_serial_ports,
    service_method,
)

from .pymata_wrapper import ArduinoBoard


class ArduinoConfig(ServiceConfig):
    """Strongly-typed config for ArduinoService.

    Fields:
      * last_port — port string of the last successful Connect. UI
        pre-selects this in the dropdown on next session.
      * last_baud — last successful baud rate. Default 115200 to match
        FirmataExpress (which is the sketch the botcore docs steer
        users toward — it's the only sketch with on-board sonar/servo
        SYSEX support). pymata4's own default is 115200 and its
        FirmataExpress handshake is conditional on baud=115200, so
        anything else falls through to a firmware-version query that
        fails noisily on a board that's actually fine.
      * autoreconnect — the persisted *desired connection state*. Set
        automatically: True after a successful connect, False after an
        explicit disconnect. On start, a board that was connected
        (autoreconnect=True + a valid last_port) re-opens itself with no
        operator action. Defaults False so a fresh instance never blindly
        opens a guessed port.
    """
    last_port: Optional[str] = None
    last_baud: int = 115200
    autoreconnect: bool = False
    # Pins under continuous streaming (Firmata REPORT mode) → the effective
    # sampling interval in ms. The KEY is the pin LABEL ('6' digital, 'A0'
    # analog); the VALUE mirrors the global ``sampling_interval_ms`` (Firmata
    # has ONE global sampling clock, so all entries stay equal to it). The pin
    # streams {value, ts} on /arduino/{id}/pin/{label} whenever Firmata pushes
    # a change — so a scope (or any consumer) gets a live trace with no
    # per-sample request. Persisted so a restart restores it; legacy values
    # (old per-pin poll intervals) migrate in place with no rewrite needed.
    pin_polls: Dict[str, int] = {}
    # Global Firmata analog sampling cadence (ms) — how often the firmware
    # samples + reports analog inputs. Floor ~10ms, 14-bit cap 16383. Default
    # 20ms (~50Hz) tracks the board-native 19ms rather than aliasing at 50ms.
    sampling_interval_ms: int = 20
    # Global analog change-gate: pymata4 only fires the stream callback when an
    # analog reading moves by >= this many counts (digital is edge-only, so it
    # ignores this). 1 = report every change.
    differential: int = 1


logger = logging.getLogger(__name__)


# Port enumeration + ownership detection lives in rlx_bus
# (``list_serial_ports``) so every subprocess service that opens a
# serial device sees the same data. Each entry carries ``holders``
# and ``available`` so the UI can grey out devices another service
# is currently using.


class ArduinoService(SubprocessService):
    """Bus-facing wrapper around the pymata4 board.

    Topic layout (inherited from SubprocessService's namespace
    convention, with this service's ``type_name='arduino'``):

        /arduino/{id}/state        retained — connection + pin snapshot
        /arduino/{id}/heartbeat    1Hz, automatic via base class
        /arduino/{id}/control      incoming actions
        /arduino/{id}/pin/{N}      per-pin value updates
        /arduino/{id}/sonar/{N}    sonar distance updates
    """

    type_name = "arduino"
    heartbeat_interval_s = 1.0   # base class publishes /arduino/{id}/heartbeat
    # Consecutive heartbeat ticks the connected board's /dev node must be
    # absent before we declare a physical unplug. Debounces a single
    # transient enumeration miss; ~2-3s to flip the UI red on a real unplug.
    _MISSING_LIMIT = 2
    config_class = ArduinoConfig

    def __init__(self, proxy_id, bus) -> None:
        super().__init__(proxy_id, bus)
        self.board = ArduinoBoard()
        self._info: Dict[str, Any] = {}
        self._connect_error: Optional[str] = None
        self._missing_ticks = 0
        # Streaming bridge state (see on_start). Declared here so any early
        # access is safe; the asyncio primitives are (re)created in on_start
        # where a running loop exists.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._active_labels: set = set()              # LOOP-THREAD source of truth
        self._pin_pending: Dict[str, tuple] = {}      # label -> (value, ts); LOOP-THREAD only
        self._stream_lock: Optional[asyncio.Lock] = None
        self._pin_wake: Optional[asyncio.Event] = None
        self._drain_task: Optional[asyncio.Task] = None

    # ─── lifecycle ───────────────────────────────────────────────────
    async def on_start(self) -> None:
        # Capture the event loop (the pymata4 reporter thread schedules onto it
        # via call_soon_threadsafe) and stand up the report→bus drain.
        self._loop = asyncio.get_running_loop()
        self._stream_lock = asyncio.Lock()
        self._pin_wake = asyncio.Event()
        self._active_labels = set()
        self._pin_pending = {}
        self._drain_task = asyncio.create_task(self._drain_pin_reports())
        self._migrate_pin_polls()
        await self.publish_state()
        # Restore the desired connection state: a board that was connected
        # when last saved (autoreconnect=True + a valid last_port) re-opens
        # itself, then re-arms every persisted stream. Survivable — a
        # stale/absent port just logs and leaves the service disconnected.
        if self.config.autoreconnect and self.config.last_port:
            try:
                self._info = await self.board.connect(
                    self.config.last_port, self.config.last_baud, bus=self.bus,
                )
                self._connect_error = None
                await self._resume_streams()
            except Exception as exc:  # noqa: BLE001
                logger.exception("arduino %s: autoreconnect to %s failed",
                                 self.proxy_id, self.config.last_port)
                self._info = {}
                self._connect_error = f"{type(exc).__name__}: {exc}"
            await self.publish_state()

    async def on_stop(self) -> None:
        # Stop the SOURCE first, then drain + cancel the SINK, so no late report
        # can publish on a closing bus. Every board call is bounded: pymata4's
        # shutdown() disables reporting on every pin + resets + closes the port,
        # which contends with an active report-mode stream and can block past
        # the process grace window (→ SIGKILL). So we (a) quiet the line by
        # disabling OUR streams first, then (b) bound disconnect so a slow board
        # can never hold the process open.
        for label in list(self._active_labels):
            try:
                await asyncio.wait_for(self._stop_stream(label, keep_reporting=False), timeout=1.0)
            except Exception:  # noqa: BLE001
                pass
        if self.board.connected:
            try:
                await asyncio.wait_for(self.board.disconnect(), timeout=2.0)
            except Exception:  # noqa: BLE001
                logger.warning("arduino %s: board disconnect slow on stop — proceeding to exit",
                               self.proxy_id)
        self._active_labels.clear()
        self._pin_pending.clear()
        self._stop_event.set()
        if self._pin_wake is not None:
            self._pin_wake.set()          # wake the drain so it can observe _stop_event
        task = self._drain_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            self.board._io.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass

    # ─── report → bus bridge (Firmata push) ──────────────────────────
    def _make_pin_callback(self, label: str):
        """Build the pymata4 callback for ``label``. It runs on pymata4's
        reporter THREAD, so it must (a) never raise — an uncaught exception
        would unwind the daemon reporter thread with no restart, permanently
        darkening the board — and (b) never touch the async bus directly; it
        only schedules onto the captured loop."""
        def _cb(message) -> None:
            try:
                value = message[2]                 # [type, pin, value, ts]
                ts = message[3]
                loop = self._loop
                if loop is None or loop.is_closed():
                    return
                loop.call_soon_threadsafe(self._ingest_pin_report, label, value, ts)
            except Exception:  # noqa: BLE001 — MUST NOT raise into the reporter thread
                return
        return _cb

    def _ingest_pin_report(self, label, value, ts) -> None:
        """Loop-thread sink for a pushed report. Coalesces (latest wins per
        label between drains) and drops stragglers for stopped/released pins."""
        if label not in self._active_labels:       # post-stop straggler → no-op
            return
        self._pin_pending[label] = (value, ts)
        self.board.pins.setdefault(label, {})["value"] = int(value)   # keep _snapshot warm
        if self._pin_wake is not None:
            self._pin_wake.set()

    async def _drain_pin_reports(self) -> None:
        """The SINGLE publisher of streamed pin values. Waits for reports,
        atomically swaps out the pending map, and publishes each on the bus."""
        try:
            while not self._stop_event.is_set():
                await self._pin_wake.wait()
                self._pin_wake.clear()
                batch, self._pin_pending = self._pin_pending, {}   # await-free swap
                for label, (value, ts) in batch.items():
                    try:
                        await self.bus.publish(self.pin_topic(label), {"value": value, "ts": ts})
                    except Exception:  # noqa: BLE001 — one bad publish must not kill the drain
                        logger.exception("arduino %s: pin publish(%s) failed", self.proxy_id, label)
        except asyncio.CancelledError:
            return

    def _migrate_pin_polls(self) -> None:
        """Legacy ``pin_polls`` held per-pin poll intervals; streaming uses ONE
        global cadence. If entries exist and the global is still default, seed
        it from the smallest legacy interval, then make all values uniform. No
        data is lost — keys (the pins) carry intent and resume as-is."""
        polls = dict(self.config.pin_polls or {})
        if not polls:
            return
        try:
            vals = [int(v) for v in polls.values() if int(v) > 0]
        except Exception:  # noqa: BLE001
            vals = []
        if vals and self.config.sampling_interval_ms == 20:
            self.config.sampling_interval_ms = max(10, min(16383, min(vals)))
        si = self.config.sampling_interval_ms
        if any(int(v) != si for v in polls.values()):
            self.config.pin_polls = {str(k): si for k in polls}

    # ─── liveness / unplug detection ─────────────────────────────────
    async def _ports(self) -> list:
        """Enumerate serial ports OFF the event loop. ``list_serial_ports()``
        reads sysfs/udev and can stall for seconds during USB churn — running
        it synchronously on the loop (as before) froze the heartbeat AND every
        streamed publish together. The DEFAULT pool is used deliberately (not
        the single board worker) so a 4s ``connect`` holding that worker can't
        defer enumeration either."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, list_serial_ports)

    async def _check_liveness(self) -> None:
        """Tear the connection down if the board's /dev node has vanished.

        pymata4 never reports a USB unplug — its reader thread swallows the
        OSError and ``board.connected`` only reflects whether ``connect()``
        ran — so a yanked cable otherwise leaves a stale ``connected: true``
        forever. Periodically we re-enumerate serial ports (off the loop, with
        a timeout) and confirm the connected device is still present. On a
        Linux CDC-ACM unplug udev removes ``/dev/ttyACM*`` so it drops out
        deterministically. ``bus:`` virtual ports have no /dev node and are
        skipped (the owning serial service reports their liveness).
        """
        if not self.board.connected:
            self._missing_ticks = 0
            return
        port = self.board.port
        if not port or port.startswith("bus:"):
            self._missing_ticks = 0
            return
        try:
            ports = await asyncio.wait_for(self._ports(), timeout=1.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            return                                  # skip this tick; never stall on enumeration
        devices = {p.get("device") for p in ports}
        if port in devices:
            self._missing_ticks = 0
            return
        # Device absent — debounce a single transient enumeration miss before
        # committing to "unplugged".
        self._missing_ticks += 1
        if self._missing_ticks < self._MISSING_LIMIT:
            return
        self._missing_ticks = 0
        logger.warning("arduino %s: device %s vanished from the port list — "
                       "treating as physical unplug", self.proxy_id, port)
        try:
            await self.board.disconnect()
        except Exception:  # noqa: BLE001
            logger.exception("arduino: disconnect during unplug-detect raised")
        self._info = {}
        self._connect_error = f"device removed: {port}"
        await self.publish_state()

    async def _heartbeat_loop(self) -> None:
        """Heartbeat that also reflects REAL board liveness.

        Overrides ``SubprocessService._heartbeat_loop`` — same cadence and
        the same ``_stop_event``-driven shutdown (still spawned once by the
        base at startup) — but probes the physical device each tick, so a
        USB unplug flips ``connected`` false within a couple of seconds
        instead of staying green forever. The probe is isolated in its own
        ``try`` so a probe failure can never silence the heartbeat.
        """
        interval = self.heartbeat_interval_s
        if not interval or interval <= 0:
            return
        tick = 0
        live_task: Optional[asyncio.Task] = None
        while not self._stop_event.is_set():
            # Publish the heartbeat FIRST and unconditionally, then IMMEDIATELY
            # sleep — the beat is only ever ``publish + sleep``, so its cadence
            # is independent of enumeration latency. Liveness runs FULLY
            # DECOUPLED as a background task (never awaited here) every 3rd tick,
            # and only if the prior one finished, so a slow/timed-out port scan
            # can neither delay nor pile up behind the 1 Hz beat.
            try:
                await self.publish("heartbeat", {"ts": time.time()})
            except Exception:  # noqa: BLE001
                logger.exception("%s: heartbeat publish failed", self.proxy_id)
            tick += 1
            if tick % 3 == 0 and (live_task is None or live_task.done()):
                live_task = asyncio.create_task(self._safe_liveness())
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                continue

    async def _safe_liveness(self) -> None:
        try:
            await self._check_liveness()
        except Exception:  # noqa: BLE001
            logger.exception("%s: liveness check failed", self.proxy_id)

    # ─── state ───────────────────────────────────────────────────────
    def _snapshot(self, ports: list) -> Dict[str, Any]:
        return {
            "connected": self.board.connected,
            "port": self.board.port if self.board.connected else None,
            "ports": ports,
            "firmata_version": self._info.get("firmata_version"),
            "firmware_name": self._info.get("firmware_name"),
            "firmware_version": self._info.get("firmware_version"),
            # Build from a snapshot copy — an executor-thread manual read/write
            # may add a key concurrently.
            "pins": dict(list(self.board.pins.items())),
            "polling": dict(self.config.pin_polls or {}),
            "sampling_interval_ms": self.config.sampling_interval_ms,
            "differential": self.config.differential,
            # Pin labels this board actually has (discovered from its Firmata
            # analog map) — the UI offers these as a typeahead picklist, and
            # falls back to free-form entry when empty (board not recognised).
            "board_pins": list(self.board.available_pins),
            # Persisted config — UI uses last_port to pre-select the
            # dropdown on next session.
            "last_port": self.config.last_port,
            "last_baud": self.config.last_baud,
            # Desired connection state (drives auto-reconnect on start).
            "autoreconnect": self.config.autoreconnect,
            # Last connect attempt's error string, or None if the most
            # recent attempt succeeded / no attempts yet. Always
            # included in /state so a successful connect publishes
            # explicit None and clears any stale red banner in the UI.
            "connect_error": self._connect_error,
        }

    async def publish_state(self) -> None:
        ports = await self._ports()
        await self.publish("state", self._snapshot(ports), retained=True)

    def pin_topic(self, pin) -> str:
        return self.topic(f"pin/{self.board.canon(pin)}")

    def sonar_topic(self, trigger: int) -> str:
        return self.topic(f"sonar/{trigger}")

    # ─── @service_method actions ─────────────────────────────────────
    @service_method("list_ports")
    async def m_list_ports(self) -> Dict[str, Any]:
        """Re-scan serial ports and republish state."""
        ports = await self._ports()
        await self.publish_state()
        return {"ports": ports}

    @service_method("connect")
    async def m_connect(self, port: str, baud: int = 115200) -> Dict[str, Any]:
        """Open the firmata board on the given port.

        Connect failures are SURVIVABLE — the service keeps running so
        the user can iterate (try a different port/baud, reset the
        board, etc.). Errors are surfaced in two ways:
          * The /state topic carries a ``connect_error`` field that the
            UI's Firmware section renders next to the Connect button.
          * The reply payload (from the CLI ``call connect`` path or
            the inspector's request-response wiring) carries the same
            error envelope so the caller sees it inline.
        The previous version re-raised — the framework's broad-exception
        catch logged a noisy stack and the caller saw "method raised"
        with no actionable detail. Now the connect error is a structured
        return value the UI + CLI can render verbatim.
        """
        if self.board.connected:
            # Defensive: ensure no half-open serial port hangs around
            # from a previous flaky attempt. Best-effort.
            try:
                await self.board.disconnect()
            except Exception:  # noqa: BLE001
                logger.exception("arduino: pre-connect cleanup raised")
        self._info = {}
        # Clear any prior connect_error so the in-flight state doesn't
        # carry the stale red banner from a previous failure.
        self._connect_error = None
        await self.publish_state()
        try:
            # Pass our BusClient so ``bus:<serial-proxy-id>`` URLs
            # can construct a BusBackedSerial that talks to this
            # service's existing bus connection.
            self._info = await self.board.connect(port, baud, bus=self.bus)
        except Exception as exc:  # noqa: BLE001
            logger.exception("arduino: connect to %s failed", port)
            self._info = {}
            self._connect_error = f"{type(exc).__name__}: {exc}"
            await self.publish_state()
            return {"connected": False, "error": self._connect_error}
        # Success — clear the error and persist port/baud + the desired
        # connection state (autoreconnect) so this board re-opens on the
        # next start.
        self._connect_error = None
        await self.update_config({"last_port": port, "last_baud": int(baud), "autoreconnect": True})
        # Re-arm every persisted stream on the freshly-opened board (Firmata
        # report mode + the sampling interval + the per-pin callbacks).
        await self._resume_streams()
        await self.publish_state()
        return self._info

    @service_method("clear_error")
    async def m_clear_error(self) -> Dict[str, Any]:
        """Dismiss any persistent ``connect_error`` on /state so the UI
        clears its red banner. Use after the user has acknowledged the
        last failure — doesn't change board state, just the display."""
        self._connect_error = None
        await self.publish_state()
        return {"cleared": True}

    @service_method("disconnect")
    async def m_disconnect(self) -> Dict[str, Any]:
        """Release the board if connected. Idempotent + tolerant of
        pymata4 shutdown errors — the goal is "after this returns,
        the service is in a known-disconnected state and ready to
        accept a fresh connect"."""
        try:
            await self.board.disconnect()
        except Exception as exc:  # noqa: BLE001
            logger.exception("arduino: disconnect raised — clearing state anyway")
        # board.disconnect() nulled every pymata callback + cleared _streamed;
        # clear the loop-side bridge residue too so a straggler can't resurrect
        # a ghost pin into _snapshot (pin_polls is kept — reconnect re-arms it).
        self._active_labels.clear()
        self._pin_pending.clear()
        self._info = {}
        # Explicit operator disconnect → clear the desired connection state
        # so the board does NOT auto-reconnect on the next start.
        await self.update_config({"autoreconnect": False})
        await self.publish_state()
        return {"connected": False}

    @service_method("set_pin_mode")
    async def m_set_pin_mode(self, pin, mode: str) -> Dict[str, Any]:
        label = self.board.canon(pin)
        # Repurposing a streamed pin (e.g. input → output/servo) must first
        # tear the stream down: null its callback, stop reporting, and drop it
        # from pin_polls, or a zombie report would keep publishing on a pin now
        # driven as an output.
        if label in (self.config.pin_polls or {}) and str(mode) not in ("input", "analog"):
            async with self._stream_lock:
                await self._stop_stream(label, keep_reporting=False)
                polls = dict(self.config.pin_polls or {})
                polls.pop(label, None)
                await self.update_config({"pin_polls": polls})
        label = await self.board.set_pin_mode(pin, str(mode))
        eff = (self.board.pins.get(label) or {}).get("mode", mode)
        await self.publish_state()
        return {"pin": label, "mode": eff}

    @service_method("digital_write")
    async def m_digital_write(self, pin, value: int) -> Dict[str, Any]:
        await self.board.digital_write(pin, int(value))
        v = int(bool(value))
        await self.bus.publish(self.pin_topic(pin), {"value": v})
        return {"pin": self.board.canon(pin), "value": v}

    @service_method("digital_read")
    async def m_digital_read(self, pin) -> Dict[str, Any]:
        v = await self.board.digital_read(pin)
        await self.bus.publish(self.pin_topic(pin), {"value": v})
        return {"pin": self.board.canon(pin), "value": v}

    @service_method("analog_read")
    async def m_analog_read(self, pin) -> Dict[str, Any]:
        _, num = self.board._split(pin)
        label = f"A{num}"                       # analog reads live under the A<n> label
        v = await self.board.analog_read(pin)
        await self.bus.publish(self.pin_topic(label), {"value": v})
        return {"pin": label, "value": v}

    @service_method("analog_write")
    async def m_analog_write(self, pin, value: int) -> Dict[str, Any]:
        v = max(0, min(255, int(value)))
        await self.board.analog_write(pin, v)
        await self.bus.publish(self.pin_topic(pin), {"value": v})
        return {"pin": self.board.canon(pin), "value": v}

    @service_method("release_pin", publishes=["state"])
    async def m_release_pin(self, pin) -> Dict[str, Any]:
        """Remove a pin — stop any stream on it (nulls the callback AND turns
        Firmata reporting off to cut serial bandwidth) and drop it from the
        pin list."""
        label = self.board.canon(pin)
        async with self._stream_lock:
            await self._stop_stream(label, keep_reporting=False)
            self.board.release(label)
            polls = dict(self.config.pin_polls or {})
            polls.pop(label, None)
            await self.update_config({"pin_polls": polls})
        await self.publish_state()
        return {"pin": label, "released": True}

    # ─── streaming (Firmata REPORT/push mode for the scope / any consumer) ──
    @service_method("poll_pin", publishes=["state"])
    async def m_poll_pin(self, pin, interval_ms: int = 100) -> Dict[str, Any]:
        """Start (or restart) STREAMING ``pin`` — a LABEL ('6' digital, 'A0'
        analog). Puts the pin in Firmata report mode: the firmware pushes each
        change and we publish ``{value, ts}`` on /arduino/{id}/pin/{label}. No
        poll loop, no per-sample request. ``interval_ms`` sets the GLOBAL
        Firmata sampling cadence (all streamed pins share one clock); 0 stops
        (same as stop_poll). Wire name kept as ``poll_pin`` for UI compat."""
        label = self.board.canon(pin)
        interval_ms = int(interval_ms)
        if interval_ms <= 0:
            return await self.m_stop_poll(pin=label)
        async with self._stream_lock:
            await self._stop_stream(label)                 # idempotent restart
            # Firmata has ONE global sampling clock; adopt this interval as the
            # global and keep every pin_polls value equal to it so the UI shows
            # the true rate.
            self.config.sampling_interval_ms = max(10, min(16383, interval_ms))
            polls = {str(k): self.config.sampling_interval_ms
                     for k in (self.config.pin_polls or {})}
            polls[label] = self.config.sampling_interval_ms
            await self.update_config({
                "pin_polls": polls,
                "sampling_interval_ms": self.config.sampling_interval_ms,
            })
            if self.board.connected:
                try:
                    await self.board.set_sampling_interval(self.config.sampling_interval_ms)
                except Exception:  # noqa: BLE001
                    logger.exception("arduino %s: set_sampling_interval failed", self.proxy_id)
                await self._start_stream(label)
        await self.publish_state()
        return {"pin": label, "interval_ms": self.config.sampling_interval_ms, "polling": True}

    @service_method("stop_poll", publishes=["state"])
    async def m_stop_poll(self, pin) -> Dict[str, Any]:
        """Stop streaming ``pin`` but LEAVE Firmata reporting on so the pymata
        cache stays warm — a manual Read still returns a live value."""
        label = self.board.canon(pin)
        async with self._stream_lock:
            await self._stop_stream(label, keep_reporting=True)
            polls = dict(self.config.pin_polls or {})
            polls.pop(label, None)
            await self.update_config({"pin_polls": polls})
        await self.publish_state()
        return {"pin": label, "polling": False}

    @service_method("set_sampling_interval", publishes=["state"])
    async def m_set_sampling_interval(self, interval_ms: int) -> Dict[str, Any]:
        """Set the GLOBAL Firmata analog sampling cadence (ms). Applies to
        every streamed pin (Firmata has one sampling clock)."""
        ms = max(10, min(16383, int(interval_ms)))
        async with self._stream_lock:
            self.config.sampling_interval_ms = ms
            polls = {str(k): ms for k in (self.config.pin_polls or {})}
            await self.update_config({"sampling_interval_ms": ms, "pin_polls": polls})
            if self.board.connected:
                try:
                    await self.board.set_sampling_interval(ms)
                except Exception:  # noqa: BLE001
                    logger.exception("arduino %s: set_sampling_interval failed", self.proxy_id)
        await self.publish_state()
        return {"sampling_interval_ms": ms}

    @service_method("list_signals")
    async def m_list_signals(self) -> Dict[str, Any]:
        """signal_source capability: advertise the polled pins as plottable
        signals so a consumer (e.g. the oscope channel picker) discovers
        them. Poll a pin (poll_pin) first so it's actively streaming."""
        signals = []
        for label in (self.config.pin_polls or {}):
            is_analog = str(label).upper().startswith("A")
            signals.append({
                "topic": self.pin_topic(label),
                "field": "value",
                "label": f"pin {label}",
                "kind": "analog" if is_analog else "digital",
                "min": 0.0,
                "max": 1023.0 if is_analog else 1.0,   # 10-bit ADC vs digital
            })
        return {"signals": signals}

    async def _start_stream(self, label: str) -> None:
        """Arm Firmata report mode for ``label`` + seed one sample. Callers
        hold ``self._stream_lock``. ``_active_labels`` is set BEFORE arming so
        the first pushed report is never dropped as a straggler."""
        self._active_labels.add(label)
        cb = self._make_pin_callback(label)
        await self.board.enable_stream(label, cb, self.config.differential)
        # Seed the current value so an already-steady pin shows its level
        # immediately (Firmata may suppress the initial report when it equals
        # pymata4's default). Real report data wins — don't overwrite a report
        # that already landed with a stale-newer timestamp.
        try:
            if label not in self._pin_pending:
                is_a = str(label).upper().startswith("A")
                v = await (self.board.analog_read(label) if is_a
                           else self.board.digital_read(label))
                if label in self._active_labels and label not in self._pin_pending:
                    self._ingest_pin_report(label, v, time.time())
        except Exception:  # noqa: BLE001
            logger.exception("arduino %s: seed read(%s) failed", self.proxy_id, label)

    async def _stop_stream(self, label: str, *, keep_reporting: bool = False) -> None:
        """Disarm ``label``: drop it from ``_active_labels`` FIRST (synchronously)
        so any in-flight report becomes a no-op, then null the pymata callback
        (and optionally stop reporting). Callers hold ``self._stream_lock``."""
        self._active_labels.discard(label)
        self._pin_pending.pop(label, None)
        await self.board.disable_stream(label, keep_reporting=keep_reporting)

    async def _resume_streams(self) -> None:
        """Re-arm every persisted stream on a freshly-connected board + (re)apply
        the sampling interval. Called after a successful connect/autoreconnect."""
        if not self.board.connected:
            return
        async with self._stream_lock:
            labels = [str(l) for l in (self.config.pin_polls or {})]
            if labels:      # don't clobber pymata's 19ms default when nothing streams
                try:
                    await self.board.set_sampling_interval(self.config.sampling_interval_ms)
                except Exception:  # noqa: BLE001
                    logger.exception("arduino %s: set_sampling_interval on resume failed",
                                     self.proxy_id)
            for label in labels:
                try:
                    await self._start_stream(label)
                except Exception:  # noqa: BLE001
                    logger.exception("arduino %s: resume stream(%s) failed", self.proxy_id, label)

    @service_method("i2c_setup")
    async def m_i2c_setup(self) -> Dict[str, Any]:
        await self.board.i2c_setup()
        return {"ok": True}

    @service_method("i2c_scan")
    async def m_i2c_scan(self) -> Dict[str, Any]:
        addrs = await self.board.i2c_scan()
        return {"addresses": [f"0x{a:02x}" for a in addrs]}

    @service_method("i2c_read")
    async def m_i2c_read(self, addr: int, reg: int, count: int) -> Dict[str, Any]:
        data = await self.board.i2c_read(int(addr), int(reg), int(count))
        return {"addr": int(addr), "reg": int(reg), "data": data}

    @service_method("i2c_write")
    async def m_i2c_write(self, addr: int, data: List[int]) -> Dict[str, Any]:
        await self.board.i2c_write(int(addr), [int(x) for x in (data or [])])
        return {"addr": int(addr), "len": len(data or [])}

    # ─── servo_controller interface ──────────────────────────────────
    # These three methods are the standard servo_controller contract.
    # Any service that declares ``implements: [servo_controller]`` in
    # its package.yml must accept these actions on its /control topic;
    # the servo service publishes them without knowing what board (or
    # protocol) is actually on the other end.
    # ``servo_id`` is part of the servo_controller wire contract (every
    # message from the servo service carries the sending servo's proxy id —
    # see repo/servo/1.0.0/servo.py). A single-board Arduino addresses the
    # servo by ``pin`` and doesn't need the id, but the handler MUST accept
    # it: the framework dispatches every payload key as a kwarg, so omitting
    # it raises "unexpected keyword argument 'servo_id'" and the call (the
    # actual set_pin_mode/servo_write to the board) is silently dropped.
    @service_method("servo_attach")
    async def m_servo_attach(self, pin, servo_id: Optional[str] = None) -> Dict[str, Any]:
        """Configure a pin for servo PWM output (a digital pin — a bare int
        from the servo_controller capability maps to a digital pin)."""
        await self.board.set_pin_mode(pin, "servo")
        await self.publish_state()
        return {"pin": self.board.canon(pin), "mode": "servo"}

    @service_method("servo_write")
    async def m_servo_write(self, pin, angle: int, servo_id: Optional[str] = None) -> Dict[str, Any]:
        """Drive a servo to ``angle`` degrees (0..180)."""
        angle = max(0, min(180, int(angle)))
        await self.board.servo_write(pin, angle)
        await self.bus.publish(self.pin_topic(pin), {"value": angle})
        return {"pin": self.board.canon(pin), "angle": angle}

    @service_method("servo_detach")
    async def m_servo_detach(self, pin, servo_id: Optional[str] = None) -> Dict[str, Any]:
        """Return a servo pin to digital-input mode (Firmata has no
        true detach; reverting to input is the conventional substitute).
        """
        await self.board.set_pin_mode(pin, "input")
        await self.publish_state()
        return {"pin": self.board.canon(pin), "mode": "input"}

    @service_method("sonar_setup")
    async def m_sonar_setup(self, trigger_pin, echo_pin: int) -> Dict[str, Any]:
        await self.board.sonar_setup(trigger_pin, echo_pin)
        await self.publish_state()
        return {"trigger": int(trigger_pin), "echo": int(echo_pin)}

    @service_method("sonar_read")
    async def m_sonar_read(self, trigger_pin: int) -> Dict[str, Any]:
        d = await self.board.sonar_read(int(trigger_pin))
        await self.bus.publish(self.sonar_topic(int(trigger_pin)), {"distance_cm": d})
        return {"trigger": int(trigger_pin), "distance_cm": d}
