"""ArduinoTelemetrixService — botcore Telemetrix arduino subprocess service.

Subclasses ``rlx_bus.SubprocessService`` so the boilerplate (env loading,
bus client, hello announcement, control dispatch, heartbeat, signals,
consume loop, graceful shutdown) lives in the framework. This module
defines only what's specific to this backend: the @service_method
actions, the state publish, and the telemetrix_wrapper integration.

Two capability interfaces are exposed (see AGENTS.md "Capability
interfaces"):

  * servo_controller — ``servo_attach(pin)`` / ``servo_write(pin, angle)``
    / ``servo_detach(pin)``. Byte-for-byte the same contract the pymata4
    ``arduino`` service implements, so a ``servo`` consumer drives either.
  * pixel_strip — addressable NeoPixel/WS2812 control. The contract:
      pixel_configure(pin, count, width=0, height=0, serpentine=False)
      pixel_set(index, r, g, b, show=True)
      pixel_set_xy(x, y, r, g, b, show=True)
      pixel_fill(r, g, b, show=True)
      pixel_clear(show=True)
      pixel_show()
      pixel_set_brightness(value)   # software, 0..255
    A pixel/LED consumer service discovers this board through the
    ``pixel_strip`` interface exactly as ``servo`` discovers
    ``servo_controller``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from rlx_bus import (
    ServiceConfig,
    SubprocessService,
    list_serial_ports,
    service_method,
)

from .telemetrix_wrapper import TelemetrixBoard


class ArduinoTelemetrixConfig(ServiceConfig):
    """Strongly-typed config for ArduinoTelemetrixService.

    Mirrors ArduinoConfig (pymata4 service):
      * last_port — port of the last successful Connect; UI pre-selects it.
      * last_baud — last successful baud (Telemetrix4Arduino runs at
        115200 by default).
      * autoreconnect — persisted desired connection state. True after a
        successful connect, False after an explicit disconnect; on start a
        board that was connected re-opens itself. Defaults False so a
        fresh instance never blindly opens a guessed port.
    """
    last_port: Optional[str] = None
    last_baud: int = 115200
    autoreconnect: bool = False


logger = logging.getLogger(__name__)


class ArduinoTelemetrixService(SubprocessService):
    """Bus-facing wrapper around a Telemetrix board.

    Topic layout (``type_name='arduino_telemetrix'``):

        /arduino_telemetrix/{id}/state        retained — connection + pin + pixel snapshot
        /arduino_telemetrix/{id}/heartbeat    1Hz, automatic via base class
        /arduino_telemetrix/{id}/control      incoming actions
        /arduino_telemetrix/{id}/pin/{N}      per-pin value updates
        /arduino_telemetrix/{id}/sonar/{N}    sonar distance updates
    """

    type_name = "arduino_telemetrix"
    heartbeat_interval_s = 1.0
    # Consecutive heartbeat ticks the connected board's /dev node must be
    # absent before we declare a physical unplug (debounces a transient
    # enumeration miss; ~2-3s to flip the UI red on a real unplug).
    _MISSING_LIMIT = 2
    config_class = ArduinoTelemetrixConfig

    def __init__(self, proxy_id, bus) -> None:
        super().__init__(proxy_id, bus)
        self.board = TelemetrixBoard()
        self._info: Dict[str, Any] = {}
        self._connect_error: Optional[str] = None
        self._missing_ticks = 0

    # ─── lifecycle ───────────────────────────────────────────────────
    async def on_start(self) -> None:
        await self.publish_state()
        if self.config.autoreconnect and self.config.last_port:
            try:
                self._info = await self.board.connect(
                    self.config.last_port, self.config.last_baud,
                )
                self._connect_error = None
            except Exception as exc:  # noqa: BLE001
                logger.exception("arduino_telemetrix %s: autoreconnect to %s failed",
                                 self.proxy_id, self.config.last_port)
                self._info = {}
                self._connect_error = f"{type(exc).__name__}: {exc}"
            await self.publish_state()

    async def on_stop(self) -> None:
        if self.board.connected:
            try:
                await self.board.disconnect()
            except Exception:  # noqa: BLE001
                logger.exception("arduino_telemetrix: disconnect raised during stop")

    # ─── liveness / unplug detection ─────────────────────────────────
    async def _check_liveness(self) -> None:
        """Tear the connection down if the board's /dev node has vanished.

        The telemetrix board, like pymata4, never reports a USB unplug —
        ``board.connected`` only reflects whether ``connect()`` ran — so a
        yanked cable otherwise leaves a stale ``connected: true`` forever.
        Once per heartbeat we re-enumerate serial ports (the SAME
        ``list_serial_ports()`` call ``_snapshot`` already makes — no serial
        I/O, so it can't false-trip during normal traffic) and confirm the
        connected device is still present. On a Linux CDC-ACM unplug udev
        removes ``/dev/ttyACM*`` so it deterministically drops out. ``bus:``
        virtual ports have no /dev node and are skipped.
        """
        if not self.board.connected:
            self._missing_ticks = 0
            return
        port = self.board.port
        if not port or port.startswith("bus:"):
            self._missing_ticks = 0
            return
        devices = {p.get("device") for p in list_serial_ports()}
        if port in devices:
            self._missing_ticks = 0
            return
        # Device absent — debounce a single transient enumeration miss.
        self._missing_ticks += 1
        if self._missing_ticks < self._MISSING_LIMIT:
            return
        self._missing_ticks = 0
        logger.warning("arduino_telemetrix %s: device %s vanished from the port "
                       "list — treating as physical unplug", self.proxy_id, port)
        try:
            await self.board.disconnect()
        except Exception:  # noqa: BLE001
            logger.exception("arduino_telemetrix: disconnect during unplug-detect raised")
        self._info = {}
        self._connect_error = f"device removed: {port}"
        await self.publish_state()

    async def _heartbeat_loop(self) -> None:
        """Heartbeat that also reflects REAL board liveness (overrides the
        base process-alive timer so a USB unplug flips ``connected`` false
        within a couple of seconds). The probe is isolated in its own
        ``try`` so a probe failure can never silence the heartbeat."""
        interval = self.heartbeat_interval_s
        if not interval or interval <= 0:
            return
        while not self._stop_event.is_set():
            try:
                await self._check_liveness()
            except Exception:  # noqa: BLE001
                logger.exception("%s: liveness check failed", self.proxy_id)
            try:
                await self.publish("heartbeat", {"ts": time.time()})
            except Exception:  # noqa: BLE001
                logger.exception("%s: heartbeat publish failed", self.proxy_id)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                continue

    # ─── state ───────────────────────────────────────────────────────
    def _snapshot(self) -> Dict[str, Any]:
        return {
            "connected": self.board.connected,
            "port": self.board.port if self.board.connected else None,
            "ports": list_serial_ports(),
            "firmata_version": self._info.get("firmata_version"),
            "firmware_name": self._info.get("firmware_name"),
            "firmware_version": self._info.get("firmware_version"),
            "pins": dict(self.board.pins),
            # pixel_strip snapshot for the UI's LED panel.
            "pixel": {
                "pin": self.board.pixel_pin,
                "count": self.board.pixel_count,
                "width": self.board.pixel_width,
                "height": self.board.pixel_height,
                "serpentine": self.board.pixel_serpentine,
                "brightness": self.board.pixel_brightness,
            },
            "last_port": self.config.last_port,
            "last_baud": self.config.last_baud,
            "autoreconnect": self.config.autoreconnect,
            "connect_error": self._connect_error,
        }

    async def publish_state(self) -> None:
        await self.publish("state", self._snapshot(), retained=True)

    def pin_topic(self, pin: int) -> str:
        return self.topic(f"pin/{pin}")

    def sonar_topic(self, trigger: int) -> str:
        return self.topic(f"sonar/{trigger}")

    # ─── connection actions ──────────────────────────────────────────
    @service_method("list_ports")
    async def m_list_ports(self) -> Dict[str, Any]:
        await self.publish_state()
        return {"ports": list_serial_ports()}

    @service_method("connect")
    async def m_connect(self, port: str, baud: int = 115200) -> Dict[str, Any]:
        """Open the Telemetrix board on the given port. Failures are
        SURVIVABLE — the error is published on /state (``connect_error``)
        and returned inline so the UI + CLI render it verbatim."""
        if self.board.connected:
            try:
                await self.board.disconnect()
            except Exception:  # noqa: BLE001
                logger.exception("arduino_telemetrix: pre-connect cleanup raised")
        self._info = {}
        self._connect_error = None
        await self.publish_state()
        try:
            self._info = await self.board.connect(port, baud)
        except Exception as exc:  # noqa: BLE001
            logger.exception("arduino_telemetrix: connect to %s failed", port)
            self._info = {}
            self._connect_error = f"{type(exc).__name__}: {exc}"
            await self.publish_state()
            return {"connected": False, "error": self._connect_error}
        self._connect_error = None
        await self.update_config({"last_port": port, "last_baud": int(baud), "autoreconnect": True})
        await self.publish_state()
        return self._info

    @service_method("clear_error")
    async def m_clear_error(self) -> Dict[str, Any]:
        self._connect_error = None
        await self.publish_state()
        return {"cleared": True}

    @service_method("disconnect")
    async def m_disconnect(self) -> Dict[str, Any]:
        try:
            await self.board.disconnect()
        except Exception:  # noqa: BLE001
            logger.exception("arduino_telemetrix: disconnect raised — clearing state anyway")
        self._info = {}
        await self.update_config({"autoreconnect": False})
        await self.publish_state()
        return {"connected": False}

    # ─── pins ────────────────────────────────────────────────────────
    @service_method("set_pin_mode")
    async def m_set_pin_mode(self, pin: int, mode: str) -> Dict[str, Any]:
        await self.board.set_pin_mode(int(pin), str(mode))
        await self.publish_state()
        return {"pin": int(pin), "mode": mode}

    @service_method("digital_write")
    async def m_digital_write(self, pin: int, value: int) -> Dict[str, Any]:
        await self.board.digital_write(int(pin), int(value))
        await self.bus.publish(self.pin_topic(int(pin)), {"value": int(bool(value))})
        return {"pin": int(pin), "value": int(bool(value))}

    @service_method("digital_read")
    async def m_digital_read(self, pin: int) -> Dict[str, Any]:
        v = await self.board.digital_read(int(pin))
        await self.bus.publish(self.pin_topic(int(pin)), {"value": v})
        return {"pin": int(pin), "value": v}

    @service_method("analog_read")
    async def m_analog_read(self, pin: int) -> Dict[str, Any]:
        v = await self.board.analog_read(int(pin))
        await self.bus.publish(self.pin_topic(int(pin)), {"value": v})
        return {"pin": int(pin), "value": v}

    @service_method("analog_write")
    async def m_analog_write(self, pin: int, value: int) -> Dict[str, Any]:
        v = max(0, min(255, int(value)))
        await self.board.analog_write(int(pin), v)
        await self.bus.publish(self.pin_topic(int(pin)), {"value": v})
        return {"pin": int(pin), "value": v}

    @service_method("i2c_setup")
    async def m_i2c_setup(self) -> Dict[str, Any]:
        await self.board.i2c_setup()
        return {"ok": True}

    @service_method("i2c_read")
    async def m_i2c_read(self, addr: int, reg: int, count: int) -> Dict[str, Any]:
        data = await self.board.i2c_read(int(addr), int(reg), int(count))
        return {"addr": int(addr), "reg": int(reg), "data": data}

    @service_method("i2c_write")
    async def m_i2c_write(self, addr: int, data: List[int]) -> Dict[str, Any]:
        await self.board.i2c_write(int(addr), [int(x) for x in (data or [])])
        return {"addr": int(addr), "len": len(data or [])}

    # ─── servo_controller interface ──────────────────────────────────
    # ``servo_id`` is part of the servo_controller wire contract (every
    # message from the servo service carries the sending servo's proxy id).
    # The board addresses the servo by ``pin``, but the handler MUST accept
    # ``servo_id`` or the framework's kwarg dispatch raises "unexpected
    # keyword argument 'servo_id'" and silently drops the board call.
    @service_method("servo_attach")
    async def m_servo_attach(self, pin: int, servo_id: Optional[str] = None) -> Dict[str, Any]:
        await self.board.servo_attach(int(pin))
        await self.publish_state()
        return {"pin": int(pin), "mode": "servo"}

    @service_method("servo_write")
    async def m_servo_write(self, pin: int, angle: int, servo_id: Optional[str] = None) -> Dict[str, Any]:
        angle = max(0, min(180, int(angle)))
        await self.board.servo_write(int(pin), angle)
        await self.bus.publish(self.pin_topic(int(pin)), {"value": angle})
        return {"pin": int(pin), "angle": angle}

    @service_method("servo_detach")
    async def m_servo_detach(self, pin: int, servo_id: Optional[str] = None) -> Dict[str, Any]:
        await self.board.servo_detach(int(pin))
        await self.publish_state()
        return {"pin": int(pin), "mode": "input"}

    # ─── pixel_strip interface ───────────────────────────────────────
    @service_method("pixel_configure")
    async def m_pixel_configure(
        self,
        pin: int,
        count: int,
        width: int = 0,
        height: int = 0,
        serpentine: bool = False,
    ) -> Dict[str, Any]:
        """Configure the board's addressable strip/matrix on ``pin`` with
        ``count`` pixels. Pass ``width``/``height`` (+ ``serpentine``) to
        enable x/y matrix addressing via pixel_set_xy."""
        await self.board.pixel_configure(
            int(pin), int(count), int(width), int(height), bool(serpentine),
        )
        await self.publish_state()
        return {
            "pin": int(pin), "count": int(count),
            "width": int(width), "height": int(height),
            "serpentine": bool(serpentine),
        }

    @service_method("pixel_set")
    async def m_pixel_set(
        self, index: int, r: int, g: int, b: int, show: bool = True,
    ) -> Dict[str, Any]:
        await self.board.pixel_set(int(index), int(r), int(g), int(b), bool(show))
        return {"index": int(index), "rgb": [int(r), int(g), int(b)]}

    @service_method("pixel_set_xy")
    async def m_pixel_set_xy(
        self, x: int, y: int, r: int, g: int, b: int, show: bool = True,
    ) -> Dict[str, Any]:
        await self.board.pixel_set_xy(int(x), int(y), int(r), int(g), int(b), bool(show))
        return {"x": int(x), "y": int(y), "rgb": [int(r), int(g), int(b)]}

    @service_method("pixel_fill")
    async def m_pixel_fill(
        self, r: int, g: int, b: int, show: bool = True,
    ) -> Dict[str, Any]:
        await self.board.pixel_fill(int(r), int(g), int(b), bool(show))
        return {"rgb": [int(r), int(g), int(b)]}

    @service_method("pixel_clear")
    async def m_pixel_clear(self, show: bool = True) -> Dict[str, Any]:
        await self.board.pixel_clear(bool(show))
        return {"cleared": True}

    @service_method("pixel_show")
    async def m_pixel_show(self) -> Dict[str, Any]:
        await self.board.pixel_show()
        return {"shown": True}

    @service_method("pixel_set_brightness")
    async def m_pixel_set_brightness(self, value: int) -> Dict[str, Any]:
        await self.board.pixel_set_brightness(int(value))
        await self.publish_state()
        return {"brightness": max(0, min(255, int(value)))}

    # ─── sonar ───────────────────────────────────────────────────────
    @service_method("sonar_setup")
    async def m_sonar_setup(self, trigger_pin: int, echo_pin: int) -> Dict[str, Any]:
        await self.board.sonar_setup(int(trigger_pin), int(echo_pin))
        await self.publish_state()
        return {"trigger": int(trigger_pin), "echo": int(echo_pin)}

    @service_method("sonar_read")
    async def m_sonar_read(self, trigger_pin: int) -> Dict[str, Any]:
        d = await self.board.sonar_read(int(trigger_pin))
        await self.bus.publish(self.sonar_topic(int(trigger_pin)), {"distance_cm": d})
        return {"trigger": int(trigger_pin), "distance_cm": d}
