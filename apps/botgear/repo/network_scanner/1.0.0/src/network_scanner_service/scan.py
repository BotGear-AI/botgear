"""LAN sweep: prime the OS ARP/neighbor cache, then read it back.

We deliberately never touch raw sockets/ICMP (no root, no scapy). The
trick: the kernel resolves ARP for any IPv4 address you attempt to
reach on a local subnet, regardless of whether the attempted connection
itself succeeds or is refused. So a bounded burst of TCP connect
attempts across a /24 "primes" the neighbor table, and a subsequent
``ip neighbor show`` (or ``ip -4 neigh show``) read-back gives us
{ip, mac, state} for anything that answered ARP — no fragile ping
parsing, no privilege escalation.

A cheap mop-up ping pass (subprocess ``ping -c1 -W1``) follows for the
handful of hosts a firewalled TCP probe won't wake, at much lower
concurrency since spawning processes is far more expensive than
opening sockets.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

# Ports likely to get *some* TCP response (even RST) from consumer/IoT
# gear without waiting out a full connect timeout: 80/443 (almost
# everything with an IP has a web UI or at least answers/refuses), 22
# (routers/NAS/linux boxes), 445 (Windows/Samba), 8080 (IoT admin UIs).
_PROBE_PORTS = (80, 443, 22, 445, 8080)

_NEIGH_LINE_RE = re.compile(
    r"^(?P<ip>\d+\.\d+\.\d+\.\d+)\s+dev\s+(?P<dev>\S+)"
    r"(?:\s+lladdr\s+(?P<mac>[0-9a-fA-F:]{17}))?"
    r"\s+(?P<state>\S+)\s*$"
)


@dataclass
class NeighborEntry:
    ip: str
    mac: Optional[str]
    interface: str
    state: str

    @property
    def reachable(self) -> bool:
        # REACHABLE/STALE/DELAY/PROBE all mean "we have a MAC for this
        # IP right now"; FAILED/INCOMPLETE mean the kernel tried and
        # got nothing back.
        return self.mac is not None and self.state.upper() not in ("FAILED", "INCOMPLETE")


async def _probe_one(ip: str, sem: asyncio.Semaphore, timeout_s: float) -> None:
    async with sem:
        for port in _PROBE_PORTS:
            try:
                fut = asyncio.open_connection(ip, port)
                reader, writer = await asyncio.wait_for(fut, timeout=timeout_s)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                return
            except Exception:
                # Refused/timed-out/unreachable are all fine — a RST
                # already means the kernel got an ARP reply, and even a
                # pure timeout has usually triggered the ARP request.
                continue


async def _ping_one(ip: str, sem: asyncio.Semaphore, timeout_s: float) -> None:
    async with sem:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ping", "-c", "1", "-W", str(max(1, int(timeout_s))), ip,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=timeout_s + 1.0)
        except Exception:
            pass


async def prime_arp_cache(
    hosts: Iterable[str],
    *,
    concurrency: int = 64,
    timeout_s: float = 0.75,
    ping_concurrency: int = 16,
    ping_timeout_s: float = 1.0,
    do_ping_mop_up: bool = True,
) -> None:
    """Best-effort: attempt a bounded-concurrency TCP connect against
    every host, then (optionally) a lower-concurrency ping mop-up.
    Never raises — a single bad host must never abort the whole sweep."""
    hosts = list(hosts)
    sem = asyncio.Semaphore(max(1, concurrency))
    await asyncio.gather(*(_probe_one(h, sem, timeout_s) for h in hosts), return_exceptions=True)
    if do_ping_mop_up:
        ping_sem = asyncio.Semaphore(max(1, ping_concurrency))
        await asyncio.gather(
            *(_ping_one(h, ping_sem, ping_timeout_s) for h in hosts),
            return_exceptions=True,
        )


def _parse_neigh_output(text: str) -> List[NeighborEntry]:
    out: List[NeighborEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _NEIGH_LINE_RE.match(line)
        if not m:
            continue
        out.append(NeighborEntry(
            ip=m.group("ip"),
            mac=(m.group("mac") or None) and m.group("mac").lower(),
            interface=m.group("dev"),
            state=m.group("state"),
        ))
    return out


async def read_neighbor_table() -> List[NeighborEntry]:
    """Read back the kernel's ARP/neighbor cache via ``ip neigh show``.
    Returns [] (never raises) if the ``ip`` command is unavailable —
    the scan loop degrades to "nothing found this cycle" rather than
    crashing the service."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ip", "-4", "neigh", "show",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except Exception:
        return []
    return _parse_neigh_output(stdout.decode("utf-8", errors="replace"))


def hosts_for_network(network: ipaddress.IPv4Network, max_hosts: int) -> List[str]:
    """Host addresses to sweep, truncated to max_hosts (never sweep an
    accidental /8 in full — a misconfigured subnet override must not
    turn into a multi-hour scan)."""
    out: List[str] = []
    for i, addr in enumerate(network.hosts()):
        if i >= max_hosts:
            break
        out.append(str(addr))
    return out


def merge_by_mac(entries: Iterable[NeighborEntry]) -> Dict[str, NeighborEntry]:
    """Collapse neighbor entries to one per MAC (keep the most-reachable
    state — later reachable entries win over an earlier stale one)."""
    by_mac: Dict[str, NeighborEntry] = {}
    for e in entries:
        if not e.mac:
            continue
        existing = by_mac.get(e.mac)
        if existing is None or (e.reachable and not existing.reachable):
            by_mac[e.mac] = e
    return by_mac
