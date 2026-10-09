"""Serial port handle for the lcus_relay service.

Mostly write-only (relay commands), plus a best-effort blocking
write+read used only for the status-query command (0xFF). This module
owns the pyserial handle and serialises all I/O behind a lock — writes
and the query's write+read must not interleave with each other.

Open/close/write/query are all dispatched to a worker thread via
``asyncio.to_thread`` because pyserial is blocking.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional


class SerialLink:
    """Owns a pyserial.Serial handle for the LCUS relay board.

    Lifecycle::

        await link.open()                     # open the port
        await link.write(b)                   # fire-and-forget command
        await link.write_read(b, n, timeout)   # query + best-effort reply
        await link.close()                     # close the port. Idempotent.
    """

    def __init__(
        self,
        *,
        port: str,
        baudrate: int,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.port = port
        self.baudrate = int(baudrate)
        self._logger = logger or logging.getLogger(__name__)
        self._serial = None  # pyserial.Serial
        # Serialise all I/O — a relay command and a status query could
        # otherwise be dispatched concurrently and interleave on the wire.
        self._io_lock = threading.Lock()

    async def open(self) -> None:
        """Open the port. No-op if already open."""
        if self._serial is not None:
            return
        import serial as pyserial  # type: ignore

        # ``exclusive=True`` takes TIOCEXCL on Linux so a second service
        # opening the same /dev/tty* fails cleanly with "Device or
        # resource busy" instead of silently sharing the device.
        # timeout=0.2 gives write_read a sane default without making a
        # forgotten blocking read hang forever; write() ignores it.
        self._serial = await asyncio.to_thread(
            pyserial.Serial,
            self.port,
            self.baudrate,
            timeout=0.2,
            write_timeout=1.0,
            exclusive=True,
        )

    async def close(self) -> None:
        """Close the port. Safe to call more than once."""
        serial = self._serial
        self._serial = None
        if serial is not None:
            try:
                await asyncio.to_thread(serial.close)
            except Exception:  # noqa: BLE001
                self._logger.exception("lcus_relay: serial close raised")

    @property
    def connected(self) -> bool:
        return self._serial is not None

    async def write(self, data: bytes) -> int:
        """Async fire-and-forget write — dispatched to a worker thread
        because pyserial.write blocks on the OS buffer."""
        if self._serial is None:
            raise RuntimeError("serial link is not open")
        return await asyncio.to_thread(self._blocking_write, data)

    def _blocking_write(self, data: bytes) -> int:
        with self._io_lock:
            if self._serial is None:
                raise RuntimeError("serial link closed during write")
            n = self._serial.write(data)
            return int(n) if n is not None else len(data)

    async def flush(self) -> None:
        """Block until the OS driver has actually finished transmitting
        everything queued so far. ``write()`` returning only means the
        bytes were accepted into the kernel's TTY output buffer, NOT
        that they've gone out over the wire yet — at 9600 baud, 4 bytes
        take ~4ms to physically transmit, and write() returns in
        microseconds. Callers that send several packets in a burst
        (e.g. relay_all) must flush + pace between them, or all of the
        packets can land in the OS buffer almost simultaneously while
        the far end is still only partway through the first one."""
        if self._serial is None:
            return
        await asyncio.to_thread(self._blocking_flush)

    def _blocking_flush(self) -> None:
        with self._io_lock:
            if self._serial is not None:
                self._serial.flush()

    async def write_read(self, data: bytes, read_len: int, timeout: float = 0.5) -> bytes:
        """Write ``data``, then blocking-read up to ``read_len`` bytes.

        Best-effort: returns whatever arrived within ``timeout`` seconds,
        which may be shorter than ``read_len`` (or empty) if the board
        doesn't answer — callers must treat a short reply as "unknown",
        not raise on it.
        """
        if self._serial is None:
            raise RuntimeError("serial link is not open")
        return await asyncio.to_thread(self._blocking_write_read, data, read_len, timeout)

    def _blocking_write_read(self, data: bytes, read_len: int, timeout: float) -> bytes:
        with self._io_lock:
            if self._serial is None:
                raise RuntimeError("serial link closed during write_read")
            self._serial.reset_input_buffer()
            self._serial.write(data)
            self._serial.timeout = timeout
            return self._serial.read(read_len)
