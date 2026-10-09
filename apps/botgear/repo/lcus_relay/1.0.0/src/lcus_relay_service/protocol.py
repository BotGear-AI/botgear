"""LCUS relay-board protocol encoder.

Pure, side-effect-free byte encoding for the LCUS family of USB relay
modules (LCUS-1/2/4/8, e.g. the NOYITO 8-channel module). Every board
in the family speaks the same trivial protocol regardless of channel
count — ``channel_count`` only limits which channel indices are valid.

Wire format — every relay command is a 4-byte packet::

    [start, channel, command, checksum]

  * start    — always 0xA0.
  * channel  — 1-based relay index (1..8, but only 1..channel_count are
               wired to a real relay on a given board).
  * command  — 0x00 "off", 0x01 "on".
  * checksum — ``(start + channel + command) & 0xFF``.

Status query — a single bare byte ``0xFF`` (not a 4-byte packet) asks
the board for live relay state; it replies with one byte per channel,
0x01 "on" / 0x00 "off", in channel order. Not every clone board
implements this, so callers should treat it as best-effort.

Keep this module dependency-free and pure so it can be unit-tested
without a serial port.
"""
from __future__ import annotations

from typing import Dict, List


# ─── framing constants ───────────────────────────────────────────────

START_BYTE = 0xA0
CMD_OFF = 0x00
CMD_ON = 0x01

# The LCUS status-query command: a single bare byte, not a 4-byte packet.
QUERY_BYTE = 0xFF

# Channel counts of the four LCUS variants this service supports. The
# protocol itself doesn't encode a channel count anywhere — a board
# simply ignores commands addressed to channels it doesn't have.
VALID_CHANNEL_COUNTS = (1, 2, 4, 8)

MIN_CHANNEL = 1
MAX_CHANNEL = 8


# ─── encoders ──────────────────────────────────────────────────────────


def checksum(channel: int, command: int) -> int:
    """The LCUS packet checksum: low 8 bits of the byte sum."""
    return (START_BYTE + channel + command) & 0xFF


def relay_packet(channel: int, on: bool) -> bytes:
    """Build the 4-byte command packet for one relay channel.

    Raises ``ValueError`` for an out-of-range channel rather than
    silently truncating — a malformed packet on a relay bank is a
    safety problem, not something to paper over.
    """
    if not (MIN_CHANNEL <= channel <= MAX_CHANNEL):
        raise ValueError(f"channel must be {MIN_CHANNEL}..{MAX_CHANNEL} (got {channel})")
    command = CMD_ON if on else CMD_OFF
    return bytes([START_BYTE, channel, command, checksum(channel, command)])


def on_packet(channel: int) -> bytes:
    """Encode "turn channel on"."""
    return relay_packet(channel, True)


def off_packet(channel: int) -> bytes:
    """Encode "turn channel off"."""
    return relay_packet(channel, False)


def query_packet() -> bytes:
    """Encode the relay-status query — a single bare 0xFF byte."""
    return bytes([QUERY_BYTE])


def parse_status(data: bytes, channel_count: int) -> Dict[int, bool]:
    """Parse a status-query reply into ``{channel: is_on}``.

    Expects exactly ``channel_count`` bytes, one per channel in order,
    each 0x01 (on) or 0x00 (off) per the datasheet. Raises
    ``ValueError`` if the reply is short — callers treat that as a
    failed (not fatal) query, since not every clone implements it.
    """
    if len(data) < channel_count:
        raise ValueError(f"expected {channel_count} status bytes, got {len(data)}")
    return {i + 1: bool(data[i]) for i in range(channel_count)}


def channel_range(channel_count: int) -> List[int]:
    """The list of valid 1-based channel indices for a given variant."""
    return list(range(1, channel_count + 1))
