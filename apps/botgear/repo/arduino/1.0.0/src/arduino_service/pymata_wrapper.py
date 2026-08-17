"""Thin facade over pymata4.

pymata4 is synchronous and spawns its own threads. The asyncio bus client
lives in the main loop. This wrapper bridges between them — every method
that touches the board runs in the default executor, so awaiting one
doesn't block the loop while pymata4's internal threads do their work.

Pin-mode strings are normalized here ('input', 'output', 'pwm', 'analog',
'servo', 'i2c') so the wire grammar stays the same regardless of which
pymata4 method name maps to which mode.

Bus-backed virtual ports
------------------------
``connect("bus:<serial-proxy-id>", baud, ...)`` routes the firmata
byte stream through a botgear ``serial`` service instead of
directly opening ``/dev/ttyXXX``. Implementation: we monkey-patch
``pymata4.pymata4.serial.Serial`` once at connect-time to dispatch
``bus:*`` URLs into a ``rlx_bus.BusBackedSerial`` (pyserial-shaped,
bus-backed). Non-bus URLs fall through to the real pyserial
``Serial`` class. pymata4 itself is untouched — it still calls
``serial.Serial(com_port, ...)`` and gets whichever class the
factory picks.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from rlx_bus import BusBackedSerial


logger = logging.getLogger(__name__)


# pymata4 imports ``serial`` at module-load and stashes the
# reference; we patch ``serial.Serial`` on that stashed module so
# every pymata4 ``serial.Serial(port, ...)`` call goes through our
# factory. Done once per process (idempotent), via a closure that
# captures the BusClient so BusBackedSerial can subscribe + publish.
_pymata_serial_patch_installed = False


def install_bus_serial_patch(bus: Any, loop: asyncio.AbstractEventLoop) -> None:
    """Replace ``pymata4.pymata4.serial.Serial`` with a dispatcher
    that returns a ``BusBackedSerial`` for ``bus:*`` port URLs and
    delegates to the real ``pyserial.Serial`` for everything else.

    Idempotent across multiple connects in the same process (the
    closure just gets refreshed with the latest bus/loop). The
    patch is on the ``serial`` module REFERENCE inside pymata4's
    namespace — other code that imports ``serial`` directly is
    unaffected.
    """
    global _pymata_serial_patch_installed
    # Lazy-import pymata4 here so a missing dependency surfaces in
    # connect() rather than at module load (mirrors the original
    # behaviour below in ArduinoBoard.connect).
    from pymata4 import pymata4 as _pm
    import serial as _real_serial_mod
    real_Serial = _real_serial_mod.Serial

    def _factory(port: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(port, str) and port.startswith("bus:"):
            proxy_id = port[len("bus:") :]
            return BusBackedSerial(
                bus=bus,
                proxy_id=proxy_id,
                baudrate=kwargs.get("baudrate", args[0] if args else 115200),
                timeout=kwargs.get("timeout", 1.0),
                loop=loop,
            )
        return real_Serial(port, *args, **kwargs)

    # Patch the ``serial`` module reference inside pymata4 only;
    # don't touch the global ``serial`` module so other consumers
    # see unmodified behaviour.
    _pm.serial.Serial = _factory  # type: ignore[attr-defined]
    _pymata_serial_patch_installed = True

# Map our mode strings to pymata4 setter method names.
_PIN_MODE_SETTERS = {
    "input":    "set_pin_mode_digital_input",
    "output":   "set_pin_mode_digital_output",
    "pwm":      "set_pin_mode_pwm_output",
    "analog":   "set_pin_mode_analog_input",
    "servo":    "set_pin_mode_servo",
}


class ArduinoBoard:
    """Wraps pymata4.Pymata4. All public methods are awaitable."""

    def __init__(self) -> None:
        self._board = None        # pymata4.Pymata4 instance, or None when disconnected
        self._lock = threading.Lock()
        # A SINGLE-worker executor dedicated to this board. Every synchronous
        # pymata4 call (connect/discover/mode-set/read/write/disconnect) runs
        # here, so serial writes are serialized — two concurrent control
        # actions can't interleave half-frames on the wire — and a slow
        # ``connect`` (which sleeps ``arduino_wait`` ~4s) can only delay other
        # BOARD I/O, never the event loop. Streamed pin values do NOT use this
        # pool at all: Firmata pushes them and pymata4's reporter thread
        # delivers them via a callback, so streaming is immune to a connect in
        # flight. Port enumeration deliberately stays on the DEFAULT pool
        # (ArduinoService._ports) so the 1 Hz liveness probe never queues
        # behind a connect either.
        self._io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="arduino-io")
        # Pins currently in Firmata report/stream mode → (is_analog, number).
        # Loop-thread only. Used to null their pymata callbacks on
        # stop/release/disconnect and to decide port-wide digital reporting.
        self._streamed: Dict[str, Tuple[bool, int]] = {}
        self.port: Optional[str] = None
        self.baud: int = 115200
        # Cached pin state, keyed by canonical LABEL ('A0' analog, '6'
        # digital) — a pin is a label, not an address. UI gets this
        # verbatim through ArduinoService.state().
        self.pins: Dict[str, Dict[str, Any]] = {}
        # Pin labels the connected board actually has, discovered from its
        # Firmata analog map ('0'..'N' digital + 'A0'..'Ak' analog). Empty
        # when disconnected or the board didn't answer — the UI then offers
        # a free-form pin field instead of a picklist.
        self.available_pins: List[str] = []

    # ─── pin labels ──────────────────────────────────────────────────
    # A pin is a LABEL, not a number: 'A0'..'A15' are analog-input pins
    # (Firmata analog channel N), '0'..'53' / 'D6' are digital pins. This
    # removes the classic ambiguity where '0' meant A0 in analog mode but
    # digital-0 in digital mode. pymata4's analog_* take the analog channel
    # while every digital op takes the digital pin number — _split maps a
    # label to (is_analog, number) so the wire grammar is human labels.
    @staticmethod
    def _split(pin: Any) -> Tuple[bool, int]:
        s = str(pin).strip().upper()
        if s.startswith("A") and s[1:].isdigit():
            return True, int(s[1:])
        if s.startswith("D") and s[1:].isdigit():
            return False, int(s[1:])
        return False, int(s)          # bare number ⇒ digital pin (ValueError on garbage)

    @staticmethod
    def canon(pin: Any) -> str:
        is_a, num = ArduinoBoard._split(pin)
        return f"A{num}" if is_a else str(num)

    # ─── helpers ─────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._board is not None

    async def _run(self, fn, *args, **kwargs):
        """Offload a synchronous pymata4 call to the dedicated single board
        worker — serializes every serial write and keeps blocking board I/O
        off the event loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._io, lambda: fn(*args, **kwargs))

    def _require(self):
        if self._board is None:
            raise RuntimeError("not connected to a board — connect() first")
        return self._board

    # ─── lifecycle ───────────────────────────────────────────────────
    async def connect(
        self,
        port: str,
        baud: int = 115200,
        arduino_wait: float = 4.0,
        bus: Any = None,
    ) -> Dict[str, Any]:
        """Open the board on `port`. Raises if pymata4 can't establish.

        ``port`` accepts two shapes:
          * ``"/dev/ttyACM0"`` (or platform equivalent) — opens the
            OS device directly via pyserial.
          * ``"bus:<serial-proxy-id>"`` — routes the firmata byte
            stream through a botgear ``serial`` service that
            already owns the OS device. Requires ``bus`` to be the
            arduino service's BusClient so the BusBackedSerial can
            subscribe + publish. See ``install_bus_serial_patch``
            for the mechanism.

        ``arduino_wait`` is how long pymata4 sleeps after opening the
        serial port before requesting the firmware version. Opening
        the port resets Uno/Mega-style boards via the DTR pin, and
        the firmware needs time to boot back up. Default is 4s —
        FirmataExpress needs at least this; we'd been passing 2 which
        produced "Firmata Sketch Firmware Version Not Found" on a
        board that was perfectly fine, just still booting.

        ``baud`` defaults to 115200 because pymata4's FirmataExpress
        identification handshake (ARE_YOU_THERE / I_AM_HERE sysex) is
        guarded by ``if baud == 115200``. Any other baud falls through
        to a firmware-version query that times out on a properly
        flashed board, surfacing as the same "Firmware Version Not
        Found" RuntimeError. FirmataExpress itself runs at 115200 by
        default — there's no good reason to deviate.
        """
        if self._board is not None:
            await self.disconnect()
        self.port = port
        self.baud = baud

        loop = asyncio.get_running_loop()

        def _open():
            # Everything here runs on the board worker, off the event loop:
            #  * import pymata4 (missing dep surfaces as a connect() error),
            #  * install the bus-serial dispatcher BEFORE pymata4 constructs
            #    its serial port (no-op for non-``bus:*`` URLs; ``bus`` must be
            #    passed for a ``bus:<proxy>`` port or its subscribe would fail),
            #  * construct the board (this sleeps ``arduino_wait`` ~4s for the
            #    DTR reset — precisely why it must not be on the loop).
            from pymata4 import pymata4
            if bus is not None:
                install_bus_serial_patch(bus, loop)
            return pymata4.Pymata4(com_port=port, baud_rate=baud, arduino_wait=arduino_wait)
        self._board = await self._run(_open)
        info = await self.board_info()
        await self._discover_pins()
        self._streamed = {}          # fresh board — no streams armed yet
        info["available_pins"] = list(self.available_pins)
        return info

    async def _discover_pins(self) -> None:
        """Best-effort: ask the board for its Firmata analog map (pymata4
        has a 4s timeout) and derive the pin LABELS the board actually has —
        non-analog pins as '<n>', analog-capable pins as 'A<channel>'. Left
        empty on timeout/failure so the UI falls back to free-form entry."""
        self.available_pins = []
        b = self._board
        if b is None:
            return
        try:
            amap = await self._run(b.get_analog_map)
        except Exception:  # noqa: BLE001
            logger.exception("arduino: get_analog_map failed — pin picklist unavailable")
            return
        if not isinstance(amap, list) or not amap:
            return
        digital: List[str] = []
        analog: List[Tuple[int, str]] = []
        for i, ch in enumerate(amap):
            # Firmata analog map: 0x7F (127) = pin is NOT analog-capable.
            if isinstance(ch, int) and ch != 127:
                analog.append((ch, f"A{ch}"))
            else:
                digital.append(str(i))
        analog.sort()
        self.available_pins = digital + [lbl for _, lbl in analog]

    async def disconnect(self) -> None:
        b = self._board
        self._board = None
        streamed = self._streamed          # capture on the loop thread
        self._streamed = {}
        self.pins.clear()
        self.available_pins = []
        if b is None:
            return

        def _shutdown():
            # pymata4.shutdown() neither nulls the per-pin callbacks nor joins
            # its (daemon) reporter thread, so a late report could otherwise
            # fire a live closure against a board we've already dropped. Null
            # every armed callback first (the .cb setter is data_lock-guarded,
            # so this is safe against the reporter thread), THEN shut down.
            for is_a, num in streamed.values():
                try:
                    (b.analog_pins if is_a else b.digital_pins)[num].cb = None
                except Exception:  # noqa: BLE001
                    pass
            b.shutdown()
        try:
            await self._run(_shutdown)
        except Exception:  # noqa: BLE001
            logger.exception("pymata4 shutdown raised")

    async def board_info(self) -> Dict[str, Any]:
        b = self._require()
        # pymata4 exposes get_firmware_version() (the sketch's reported
        # version — includes the firmware NAME like "FirmataExpress.ino")
        # and get_protocol_version() (the Firmata protocol, e.g. "2.5").
        # Both are METHODS, not attributes. Calls block briefly while
        # pymata4 reads from its internal buffers — run in a thread so
        # we don't stall the event loop.
        info: Dict[str, Any] = {"port": self.port, "baud": self.baud}

        def _fmt(value: Any) -> Any:
            """Stringify a version-ish value. pymata4 may return a tuple
            (2, 5), a list, or already a string."""
            if value is None:
                return None
            if isinstance(value, (tuple, list)):
                return ".".join(str(x) for x in value)
            return str(value)

        try:
            fw_raw = await self._run(b.get_firmware_version)
            fw_str = _fmt(fw_raw)
            info["firmware_version"] = fw_str
            info["firmware_name"] = fw_str
            # Real-world pymata4 reports the firmware ID as
            # "<major>.<minor> <SketchName>.ino" — e.g. "1.2 FirmataExpress.ino".
            # Split the leading dotted-numeric chunk off as the version
            # and treat the rest as the name. Falls back to the raw
            # string for both when the format doesn't match (so UI
            # always shows *something* useful).
            if isinstance(fw_str, str) and " " in fw_str:
                ver, _, name = fw_str.partition(" ")
                ver = ver.strip(); name = name.strip()
                if ver.replace(".", "").isdigit() and name:
                    info["firmware_version"] = ver
                    info["firmware_name"] = name
        except Exception:  # noqa: BLE001
            logger.exception("board_info: get_firmware_version failed")
            info["firmware_version"] = None
            info["firmware_name"] = None

        try:
            proto = await self._run(b.get_protocol_version)
            info["firmata_version"] = _fmt(proto)
        except Exception:  # noqa: BLE001
            logger.exception("board_info: get_protocol_version failed")
            info["firmata_version"] = None

        return info

    # ─── pins ────────────────────────────────────────────────────────
    async def set_pin_mode(self, pin: Any, mode: str) -> str:
        """Set a pin's mode. An analog pin ('A<n>') is an analog input — the
        label carries the type; a digital pin takes input/output/pwm/servo.
        Returns the canonical label."""
        b = self._require()
        is_a, num = self._split(pin)
        label = self.canon(pin)
        if is_a:
            await self._run(b.set_pin_mode_analog_input, num)
            eff = "analog"
        else:
            if mode == "analog":
                raise ValueError(f"pin {label} is digital; use an 'A<n>' label for analog input")
            setter_name = _PIN_MODE_SETTERS.get(mode)
            if setter_name is None:
                raise ValueError(f"unknown pin mode {mode!r}")
            await self._run(getattr(b, setter_name), num)
            eff = mode
        self.pins.setdefault(label, {})["mode"] = eff
        return label

    # ─── streaming (Firmata report/push mode) ────────────────────────
    async def enable_stream(self, pin: Any, callback, differential: int = 1) -> str:
        """Put ``pin`` into Firmata REPORT mode with ``callback``. Firmata then
        streams the value at the sampling interval and pymata4's reporter
        thread invokes ``callback([type, pin, value, ts])`` on each qualifying
        change — no per-sample serial request. Returns the canonical label."""
        b = self._require()
        is_a, num = self._split(pin)
        label = self.canon(pin)
        if is_a:
            await self._run(b.set_pin_mode_analog_input, num, callback, int(differential))
            self.pins.setdefault(label, {})["mode"] = "analog"
        else:
            await self._run(b.set_pin_mode_digital_input, num, callback)
            self.pins.setdefault(label, {})["mode"] = "input"
        self._streamed[label] = (is_a, num)
        return label

    async def disable_stream(self, pin: Any, *, keep_reporting: bool = False) -> None:
        """Stop delivering reports for a streamed pin by nulling its pymata
        callback. ``keep_reporting=True`` leaves Firmata reporting ON so the
        pymata cache stays warm for a manual Read (the stop_poll path);
        ``False`` also turns reporting off to cut serial bandwidth (release).
        The sibling-port decision is computed HERE on the loop thread and
        captured, so the executor closure never iterates ``self._streamed``."""
        b = self._board
        label = self.canon(pin)
        entry = self._streamed.pop(label, None)
        if b is None or entry is None:
            return
        is_a, num = entry
        first_analog = getattr(b, "first_analog_pin", 0) or 0
        # Which digital port owns this pin's reporting? Analog channel N maps to
        # digital pin (first_analog + N); a digital pin maps to itself.
        dpin = (first_analog + num) if is_a else num
        dport = dpin // 8
        # Only cut the port-wide digital report if no OTHER streamed pin still
        # lives in the same digital port.
        keep_dport = any(
            (((getattr(b, "first_analog_pin", 0) or 0) + n) if a else n) // 8 == dport
            for a, n in self._streamed.values()
        )

        def _off():
            try:
                (b.analog_pins if is_a else b.digital_pins)[num].cb = None
            except Exception:  # noqa: BLE001
                pass
            if keep_reporting:
                return
            try:
                if is_a:
                    b.disable_analog_reporting(num)
                    # disable_analog_reporting re-modes the A-pin to a digital
                    # input, which enables the WHOLE digital port — undo that
                    # unless a sibling still needs the port.
                    if not keep_dport:
                        b.disable_digital_reporting(dpin)
                elif not keep_dport:
                    b.disable_digital_reporting(dpin)
            except Exception:  # noqa: BLE001
                logger.exception("arduino: disable_stream(%s) reporting-off raised", label)
        await self._run(_off)

    async def set_sampling_interval(self, ms: int) -> None:
        """Tell the firmware how often to sample+report analog inputs. Firmata
        floors this near 10 ms; the field is 14-bit so cap at 16383."""
        b = self._require()
        await self._run(b.set_sampling_interval, max(10, min(16383, int(ms))))

    def release(self, pin: Any) -> None:
        """Forget a pin — drop it from the cache so it leaves the UI's pin
        list. Firmata has no real pin release; the physical state stays as
        last set (the caller can set it to input first if that matters)."""
        self.pins.pop(self.canon(pin), None)

    async def digital_write(self, pin: Any, value: int) -> None:
        b = self._require()
        is_a, num = self._split(pin)
        if is_a:
            raise ValueError(f"cannot digital_write analog pin {self.canon(pin)}")
        v = 1 if value else 0
        await self._run(b.digital_write, num, v)
        self.pins.setdefault(str(num), {})["value"] = v

    async def digital_read(self, pin: Any) -> int:
        b = self._require()
        is_a, num = self._split(pin)
        if is_a:
            raise ValueError(f"cannot digital_read analog pin {self.canon(pin)}; use analog read")
        result = await self._run(b.digital_read, num)
        # pymata4 returns [value, timestamp] (a LIST, not a tuple).
        value = result[0] if isinstance(result, (tuple, list)) else int(result)
        self.pins.setdefault(str(num), {})["value"] = int(value)
        return int(value)

    async def analog_read(self, pin: Any) -> int:
        b = self._require()
        _, num = self._split(pin)   # 'A<n>' or bare number → analog channel n
        result = await self._run(b.analog_read, num)
        value = result[0] if isinstance(result, (tuple, list)) else int(result)
        self.pins.setdefault(f"A{num}", {})["value"] = int(value)
        return int(value)

    async def analog_write(self, pin: Any, value: int) -> None:
        b = self._require()
        _, num = self._split(pin)   # PWM is a digital pin
        value = max(0, min(255, int(value)))
        await self._run(b.pwm_write, num, value)
        self.pins.setdefault(str(num), {})["value"] = value

    async def servo_write(self, pin: Any, angle: int) -> None:
        """Write a servo angle. Pin must be in servo mode first.

        pymata4 distinguishes servo_write from pwm_write — for servo
        pins this calls the Firmata servo SYSEX, not the analog
        write. Angles clamp to 0..180; if a fancier servo needs more
        range, set_pin_mode_servo accepts min/max pulse-width
        overrides and we can plumb those through later.
        """
        b = self._require()
        _, num = self._split(pin)   # servo on a digital pin
        angle = max(0, min(180, int(angle)))
        await self._run(b.servo_write, num, angle)
        self.pins.setdefault(str(num), {})["value"] = angle
        # Don't change cached mode — caller should have called set_pin_mode("servo") first

    # ─── i2c ─────────────────────────────────────────────────────────
    async def i2c_setup(self) -> None:
        b = self._require()
        await self._run(b.set_pin_mode_i2c)

    async def i2c_scan(self) -> List[int]:
        """Probe 0x03..0x77 by attempting a 1-byte read; collect responders.

        Naive but works on most boards. Some firmata builds add a native
        i2c_scan — we don't depend on it.
        """
        b = self._require()
        found: List[int] = []
        for addr in range(0x03, 0x78):
            try:
                # short read; suppress exception → not present
                await self._run(b.i2c_read, addr, 0, 1, _noop_cb)
                found.append(addr)
            except Exception:
                continue
        return found

    async def i2c_read(self, addr: int, reg: int, count: int) -> List[int]:
        b = self._require()
        out: List[int] = []
        evt = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _cb(data):
            # pymata4 invokes from its own thread; bounce to loop
            try:
                # data shape varies by pymata4 version; usually [addr, reg, *bytes, ts]
                bytes_ = list(data[2:-1]) if isinstance(data, list) and len(data) > 3 else list(data or [])
                out.extend(bytes_)
            finally:
                loop.call_soon_threadsafe(evt.set)

        await self._run(b.i2c_read, addr, reg, count, _cb)
        try:
            await asyncio.wait_for(evt.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            raise RuntimeError(f"i2c_read 0x{addr:02x} reg 0x{reg:02x} timed out")
        return out

    async def i2c_write(self, addr: int, data: List[int]) -> None:
        b = self._require()
        await self._run(b.i2c_write, addr, list(data))

    # ─── sonar ───────────────────────────────────────────────────────
    async def sonar_setup(self, trigger: Any, echo: Any) -> None:
        b = self._require()
        _, tnum = self._split(trigger)
        _, enum = self._split(echo)
        await self._run(b.set_pin_mode_sonar, tnum, enum)
        self.pins.setdefault(str(tnum), {})["mode"] = "sonar_trigger"
        self.pins.setdefault(str(enum), {})["mode"] = "sonar_echo"

    async def sonar_read(self, trigger: int) -> float:
        b = self._require()
        result = await self._run(b.sonar_read, trigger)
        distance = result[0] if isinstance(result, tuple) else float(result)
        return float(distance)


def _noop_cb(_data) -> None:
    """No-op callback used by i2c_scan probes."""
    return None
