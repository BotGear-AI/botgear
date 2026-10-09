"""Best-effort device-name enrichment: reverse DNS + mDNS/zeroconf.

Reimplemented locally rather than importing ``botgear.runtime.mdns`` —
this is a subprocess service in its own venv/process and has no access
to the backend's in-process objects; it only ever talks over the bus.
The zeroconf browsing here is also a different job than that module's:
that one resolves *other botgear runtimes* specifically, this one just
wants "does anything on the LAN advertise a friendly name for this IP",
across whatever service types the LAN actually has to offer.
"""
from __future__ import annotations

import asyncio
import socket
from typing import Dict, List, Optional, Set

from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf, IPVersion

# A small curated floor, kept as a fallback alongside whatever the DNS-SD
# meta-query below actually finds — belt-and-suspenders in case multicast
# is flaky enough that the meta-query window misses types that are
# genuinely there. Confirmed live on a real home LAN that the meta-query
# alone already surfaces far more than this (_amzn-alexa, _device-info,
# _smb, _printer, _scanner, vendor-specific 3D-printer types, etc.).
_BROWSE_TYPES = (
    "_http._tcp.local.",
    "_workstation._tcp.local.",
    "_googlecast._tcp.local.",
    "_airplay._tcp.local.",
    "_ipp._tcp.local.",
)

# The DNS-SD "meta-query" service type (RFC 6763 §9): browsing it doesn't
# return service instances, it returns PTR records whose target IS the
# name of a real service type currently being advertised on the LAN —
# e.g. browsing this yields "_amzn-alexa._tcp.local." as a "name". That
# lets enrichment ask the network what's actually out there instead of
# guessing a fixed list of common types up front.
_META_QUERY_TYPE = "_services._dns-sd._udp.local."


async def reverse_dns(ip: str, *, timeout_s: float = 1.5) -> Optional[str]:
    """socket.gethostbyaddr wrapped in an executor + timeout — this call
    is blocking and can hang for the timeout's duration on an unanswered
    PTR query, so it must never run on the event loop thread directly."""
    loop = asyncio.get_running_loop()
    try:
        name, _aliases, _addrs = await asyncio.wait_for(
            loop.run_in_executor(None, socket.gethostbyaddr, ip),
            timeout=timeout_s,
        )
        return name.rstrip(".")
    except Exception:
        return None


class _TypeDiscovery:
    """Browses the DNS-SD meta-query type to learn which real service
    types are currently advertised on the LAN. Short-lived, one instance
    per enrichment pass — same reasoning as ``_MdnsNameCollector``."""

    def __init__(self) -> None:
        self.types: Set[str] = set()

    def _on_change(self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange) -> None:
        if state_change is ServiceStateChange.Removed:
            return
        if name:
            self.types.add(name)

    async def discover(self, zc: Zeroconf, *, window_s: float) -> Set[str]:
        loop = asyncio.get_running_loop()
        browser = await loop.run_in_executor(
            None, lambda: ServiceBrowser(zc, _META_QUERY_TYPE, handlers=[self._on_change])
        )
        try:
            await asyncio.sleep(window_s)
        finally:
            await loop.run_in_executor(None, browser.cancel)
        return set(self.types)


class _MdnsNameCollector:
    """Passive mDNS listener: browses whatever service types the LAN
    actually advertises (discovered via the DNS-SD meta-query, unioned
    with the curated floor as a safety net) for one sweep window and
    records ip -> friendly name from each advertisement's server/
    instance name. Short-lived by design — a fresh instance per
    enrichment pass, torn down immediately after, so it never
    accumulates stale zeroconf state across scan cycles."""

    def __init__(self) -> None:
        self.by_ip: Dict[str, str] = {}
        self._zc: Optional[Zeroconf] = None
        self._browsers: List[ServiceBrowser] = []

    def _on_change(self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange) -> None:
        if state_change is ServiceStateChange.Removed:
            return
        try:
            info = zeroconf.get_service_info(service_type, name, timeout=800)
        except Exception:
            return
        if not info:
            return
        friendly = name.split(".")[0] if name else None
        if not friendly:
            return
        try:
            addresses = info.parsed_scoped_addresses(version=IPVersion.V4Only)
        except Exception:
            addresses = []
        for addr in addresses:
            self.by_ip.setdefault(addr, friendly)

    async def collect(self, *, window_s: float, discover_types_s: float = 1.0) -> Dict[str, str]:
        loop = asyncio.get_running_loop()
        self._zc = zc = await loop.run_in_executor(None, lambda: Zeroconf(ip_version=IPVersion.V4Only))
        try:
            discovered = await _TypeDiscovery().discover(zc, window_s=discover_types_s)
            browse_types = set(_BROWSE_TYPES) | discovered
            self._browsers = await loop.run_in_executor(
                None, lambda: [ServiceBrowser(zc, t, handlers=[self._on_change]) for t in browse_types]
            )
            await asyncio.sleep(window_s)
        finally:
            for browser in self._browsers:
                await loop.run_in_executor(None, browser.cancel)
            await loop.run_in_executor(None, zc.close)
        return dict(self.by_ip)


async def mdns_enrich(*, window_s: float = 2.0, discover_types_s: float = 1.0) -> Dict[str, str]:
    """One short passive mDNS browse window -> {ip: friendly_name}.
    Best-effort: any zeroconf failure (no multicast support in this
    network namespace, etc) yields an empty dict rather than raising."""
    try:
        collector = _MdnsNameCollector()
        return await collector.collect(window_s=window_s, discover_types_s=discover_types_s)
    except Exception:
        return {}
