"""Tasmota HTTP command-API client + response parsing.

Pure protocol logic, kept separate from ``TasmotaService`` so it's
testable with no bus/asyncio-service plumbing (mirrors arduino_service's
``pymata_wrapper.py`` / ``service.py`` split).

Tasmota (https://tasmota.github.io) exposes one HTTP GET command API —
``http://<host>/cm?cmnd=<command>`` — that is IDENTICAL across every
device it runs on, regardless of manufacturer (Sonoff, Shelly boards
reflashed with Tasmota, Athom, BlitzWolf, generic ESP8266/ESP32 relay
boards, ...). That's the whole point of targeting Tasmota rather than a
specific product: one client talks to all of them, and relay count +
any sensor capability is discovered at runtime from a single ``Status
0`` call instead of hardcoded per device.

Reference (fetched from https://tasmota.github.io/docs/ during design):
  * ``Power<n> ON|OFF|TOGGLE`` — n is always given explicitly (1..8).
    Tasmota treats bare ``Power`` as an alias for ``Power1``, so always
    emitting the numbered form removes any ambiguity on multi-relay
    devices with no cost on single-relay ones.
  * ``PulseTime<n> <value>`` — auto-off timer for relay n, encoded as
    1-111 = 0.1s increments (up to 11.1s) or 112-64900 = 1s increments
    offset by 100 (i.e. value = 100 + seconds, up to ~18h). Followed by
    ``Power<n> ON`` to arm it — this is the DEVICE's own timer, so the
    relay switches back off on schedule even if botgear crashes mid-pulse.
  * ``Status 0`` — one JSON response carrying ``Status`` (module,
    FriendlyName list — index 0 is the device name, or per-relay names
    FriendlyName1/2/... on multi-relay devices), ``StatusSTS`` (POWER /
    POWER1..POWER8 — whichever keys are PRESENT is how relay count is
    auto-detected), and ``StatusSNS`` (sensor telemetry — energy,
    temperature, etc. — present only on devices that have it).
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx


def build_command_url(
    host: str, cmnd: str, *, username: Optional[str] = None, password: Optional[str] = None,
) -> str:
    """``http://<host>/cm?cmnd=<cmnd>[&user=..&password=..]``.

    Tasmota's docs call for encoding space as ``+``/``%20`` in a raw
    GET; ``urllib.parse.quote`` (default safe set) handles that and
    every other reserved character correctly.
    """
    query = f"cmnd={quote(cmnd)}"
    if username:
        query += f"&user={quote(username)}"
    if password:
        query += f"&password={quote(password)}"
    return f"http://{host}/cm?{query}"


def ms_to_pulsetime(ms: int) -> int:
    """Plain millisecond duration → Tasmota's PulseTime encoding."""
    seconds = max(0.0, ms / 1000.0)
    if seconds <= 11.1:
        return max(1, min(111, round(seconds * 10)))
    return max(112, min(64900, 100 + round(seconds)))


def _is_on(value: Any) -> bool:
    return str(value).strip().upper() in ("ON", "1", "TRUE")


def parse_relays(
    status_sts: Dict[str, Any], friendly_names: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Auto-detect relay count + state from a ``StatusSTS`` payload.

    A single-relay device reports bare ``POWER``; a multi-relay device
    reports ``POWER1``..``POWER8`` (no bare ``POWER``). Either shape
    normalizes to 1-based ``index`` here — this is what lets one
    service handle a Sonoff Basic (1 relay) and an 8CH Pro identically.
    ``friendly_names`` is ``Status.FriendlyName`` verbatim — Tasmota
    indexes per-relay names there (``FriendlyName1`` is names[0], etc.),
    so a real device-configured label flows through for free.
    """
    names = friendly_names or []

    def _label(idx: int) -> str:
        if idx - 1 < len(names) and names[idx - 1]:
            return names[idx - 1]
        return f"Relay {idx}"

    if "POWER" in status_sts:
        return [{"index": 1, "on": _is_on(status_sts["POWER"]), "label": _label(1)}]
    relays = []
    for i in range(1, 9):
        key = f"POWER{i}"
        if key in status_sts:
            relays.append({"index": i, "on": _is_on(status_sts[key]), "label": _label(i)})
    return relays


def is_tasmota_status(payload: Any) -> bool:
    """True if ``payload`` looks like a Tasmota ``Status`` JSON response.
    Used both to validate a Connect and to classify a scan probe."""
    return isinstance(payload, dict) and isinstance(payload.get("Status"), dict)


class TasmotaClient:
    """Thin async wrapper around one device's HTTP command API. One
    short-lived ``httpx.AsyncClient`` per call — these are infrequent
    (a poll every few seconds, a user-triggered action), so the
    connection-reuse a persistent client would buy isn't worth holding
    a socket open to a device that might be power-cycled at any time."""

    def __init__(
        self, host: str, *, username: Optional[str] = None,
        password: Optional[str] = None, timeout_s: float = 3.0,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.timeout_s = timeout_s

    async def _get(self, cmnd: str) -> Dict[str, Any]:
        url = build_command_url(self.host, cmnd, username=self.username, password=self.password)
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()

    async def status(self) -> Dict[str, Any]:
        """Full JSON status in one round trip: ``Status`` (module/name),
        ``StatusSTS`` (relay + system state), ``StatusSNS`` (sensors, if
        the device reports any)."""
        return await self._get("Status 0")

    async def set_power(self, relay: int, on: bool) -> Dict[str, Any]:
        return await self._get(f"Power{int(relay)} {'ON' if on else 'OFF'}")

    async def toggle_power(self, relay: int) -> Dict[str, Any]:
        return await self._get(f"Power{int(relay)} TOGGLE")

    async def pulse(self, relay: int, ms: int) -> Dict[str, Any]:
        pulsetime = ms_to_pulsetime(ms)
        await self._get(f"PulseTime{int(relay)} {pulsetime}")
        return await self._get(f"Power{int(relay)} ON")


async def _probe_one(client: httpx.AsyncClient, host: str) -> Optional[Dict[str, Any]]:
    """One unauthenticated probe. Never raises — a subnet sweep hits
    hundreds of hosts that aren't Tasmota (or aren't even listening) at
    all, and every failure mode (timeout, connection refused, non-JSON,
    JSON without a Status key) means the same thing: "not a Tasmota
    device here", not an error worth surfacing."""
    try:
        resp = await client.get(build_command_url(host, "Status 0"))
        if resp.status_code != 200:
            return None
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if not is_tasmota_status(payload):
        return None
    status = payload.get("Status") or {}
    friendly = status.get("FriendlyName") or []
    return {
        "host": host,
        "friendly_name": friendly[0] if friendly else None,
        "module": status.get("Module"),
    }


async def scan_subnet(
    subnet_prefix: str,
    *,
    concurrency: int = 50,
    timeout_s: float = 1.0,
    client_factory: Optional[Any] = None,
    host_suffixes: Optional[range] = None,
) -> List[Dict[str, Any]]:
    """Probe every host in ``<subnet_prefix>.0/24`` for a Tasmota device.

    Unauthenticated only — a device with the web-admin password set
    won't answer this probe and must be added via ``connect`` with
    credentials directly. That's an accepted limitation of a best-effort
    LAN sweep (Tasmota's own mDNS discovery was deprecated + disabled by
    default years ago, so an active HTTP scan is the practical
    alternative), not a bug to work around.

    ``client_factory`` / ``host_suffixes`` are test-only injection
    points (mirrors the ``zc_factory`` pattern used elsewhere in this
    codebase for the same reason) — production always sweeps the full
    1..254 range through a real ``httpx.AsyncClient``.
    """
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=timeout_s))
    suffixes = host_suffixes if host_suffixes is not None else range(1, 255)
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(client: httpx.AsyncClient, host: str) -> Optional[Dict[str, Any]]:
        async with sem:
            return await _probe_one(client, host)

    async with factory() as client:
        hosts = [f"{subnet_prefix}.{i}" for i in suffixes]
        results = await asyncio.gather(*(_bounded(client, h) for h in hosts))
    return [r for r in results if r is not None]
