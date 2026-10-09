"""NetworkScannerService — botgear LAN device-discovery subprocess service.

Implements the ``device_directory`` capability interface (see AGENTS.md's
"Promoted interfaces today" table): a queryable roster of every device
this box has seen on its local subnets, enriched with vendor/hostname/
mDNS-name where available, and a user-editable label/notes/type layer
on top — so ``192.168.1.38`` can become "the dishwasher" once, and stay
that way across every future scan and every service that asks.

Subprocess service (not builtin) because a scan cycle shells out to
``ip``/``ping`` and opens many concurrent TCP probes per sweep — exactly
the kind of bursty, blocking-adjacent I/O the other hardware services
here (tasmota, arduino, video) already isolate into their own process
rather than risk stalling the backend event loop.

Deliberately NOT ``singleton: true`` — see ``catalog.py``'s
``_ensure_singleton_proxy``, which unconditionally marks a singleton
proxy "running" the moment its manifest is registered, without ever
starting it. That shortcut only holds for ``language: builtin``
services (the comment there says as much: "the backend IS this
service"); a subprocess singleton flagged that way would show as
running in the UI forever without a process behind it.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from rlx_bus import ServiceConfig, SubprocessService, service_method

from . import ifaces, oui
from .enrich import mdns_enrich, reverse_dns
from .scan import NeighborEntry, hosts_for_network, merge_by_mac, prime_arp_cache, read_neighbor_table

logger = logging.getLogger(__name__)

# Concurrency for the TCP-probe priming phase. Independent of
# ping_concurrency (config) since opening sockets is far cheaper than
# spawning ping subprocesses — see scan.py's module docstring.
_SWEEP_CONCURRENCY = 64

_VALID_EVENTS = ("new", "offline", "online")


class KnownDeviceRecord(BaseModel):
    """System-writable: what the scanner itself observed. Rebuilt from
    scratch every cycle for devices currently seen, carried forward
    (with an incrementing miss counter) for devices that go quiet."""
    mac: str
    last_ip: Optional[str] = None
    interface: Optional[str] = None
    first_seen: float
    last_seen: float
    online: bool = True
    missed_scans: int = 0
    offline_since: Optional[float] = None
    hostname: Optional[str] = None
    vendor: Optional[str] = None
    mdns_name: Optional[str] = None


class UserLabel(BaseModel):
    """User-writable: never touched by the scan loop itself, only by
    ``set_label``/``remove_device``. Kept as a separate persisted map
    (keyed by the same MAC) rather than folded into
    ``KnownDeviceRecord`` so a re-scan can never clobber a label."""
    mac: str
    label: Optional[str] = None
    notes: Optional[str] = None
    device_type: Optional[str] = None
    ignored: bool = False
    trusted: bool = False
    updated_at: Optional[float] = None


class NetworkScannerConfig(ServiceConfig):
    known_devices: Dict[str, KnownDeviceRecord] = Field(default_factory=dict)
    labels: Dict[str, UserLabel] = Field(default_factory=dict)
    scan_interval_s: int = 60
    offline_grace_cycles: int = 3
    max_sweep_hosts: int = 1024
    ping_concurrency: int = 32
    subnets: List[str] = Field(default_factory=list)
    enable_vendor_lookup: bool = True
    enable_rdns: bool = True
    enable_mdns_enrich: bool = True
    alert_target: Optional[str] = None
    alert_on: List[str] = Field(default_factory=lambda: ["new", "offline", "online"])


# ── wire schemas for what this service PUBLISHES ──────────────────────

class DeviceEntry(KnownDeviceRecord):
    """One device as published — the scanner's observation joined with
    the user's label layer."""
    label: Optional[str]
    notes: Optional[str]
    device_type: Optional[str]
    ignored: bool
    trusted: bool
    display_name: str = Field(description="label, else hostname / mDNS name / vendor / MAC")


class NetworkScannerState(BaseModel):
    """Retained on ``/network_scanner/{id}/state`` — full roster (online
    first) + summary counts."""
    devices: List[DeviceEntry]
    total: int
    online: int
    offline: int
    subnets: List[str] = Field(description="CIDRs of the local subnets")
    scan_interval_s: int
    last_scan: Optional[float]
    alert_target: Optional[str]
    alert_on: List[str]


class NetworkScannerService(SubprocessService):
    """Publishes ``state`` plus one-shot ``device_new`` (first sighting
    of a MAC), ``device_online`` (reappeared) and ``device_offline``
    (crossed offline_grace_cycles misses) events, each a ``DeviceEntry``."""

    type_name = "network_scanner"
    heartbeat_interval_s = 1.0
    config_class = NetworkScannerConfig
    publishes = ["state", "device_new", "device_online", "device_offline"]
    state_schema = NetworkScannerState
    topic_schemas = {"device_new": DeviceEntry, "device_online": DeviceEntry, "device_offline": DeviceEntry}

    def __init__(self, proxy_id, bus) -> None:
        super().__init__(proxy_id, bus)
        self._scan_task: Optional[asyncio.Task] = None
        self._last_scan_at: Optional[float] = None
        self._scanning = False

    # ─── lifecycle ───────────────────────────────────────────────────
    async def on_start(self) -> None:
        await self.publish_state()
        self._scan_task = asyncio.create_task(self._scan_loop())

    async def on_stop(self) -> None:
        if self._scan_task is not None:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ─── scan loop ───────────────────────────────────────────────────
    async def _scan_loop(self) -> None:
        try:
            while not self.is_stopping():
                try:
                    await self._scan_once()
                except Exception:  # noqa: BLE001
                    logger.exception("%s: scan cycle failed", self.proxy_id)
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=max(5, self.config.scan_interval_s),
                    )
                    return
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            return

    def _target_networks(self):
        if self.config.subnets:
            nets = [n for n in (ifaces.parse_subnet_override(s) for s in self.config.subnets) if n is not None]
            if nets:
                return nets
        return [s.network for s in ifaces.local_subnets()]

    async def _scan_once(self) -> Dict[str, Any]:
        if self._scanning:
            # A manual scan_now landing mid-cycle just waits for the
            # loop's own cycle rather than running two sweeps at once
            # (each opens dozens of concurrent sockets — no reason to
            # double that up).
            return self._snapshot()
        self._scanning = True
        try:
            networks = self._target_networks()
            all_hosts: List[str] = []
            for net in networks:
                all_hosts.extend(hosts_for_network(net, self.config.max_sweep_hosts))
            if all_hosts:
                await prime_arp_cache(
                    all_hosts,
                    concurrency=_SWEEP_CONCURRENCY,
                    ping_concurrency=max(1, self.config.ping_concurrency),
                )
            neighbors = await read_neighbor_table()
            by_mac = merge_by_mac(neighbors)

            # ip neigh show structurally never contains this box's OWN
            # address (the kernel doesn't ARP itself), so without this
            # the scanner's own host silently never appears in its own
            # roster no matter how many cycles run. Synthesize one
            # "neighbor" per local interface from sysfs instead — a real
            # entry, not a special case downstream.
            self_macs: Dict[str, str] = {}
            for subnet in ifaces.local_subnets():
                if not subnet.mac:
                    continue
                by_mac.setdefault(subnet.mac, NeighborEntry(
                    ip=subnet.ip, mac=subnet.mac, interface=subnet.interface, state="REACHABLE",
                ))
                self_macs[subnet.mac] = socket.gethostname()

            mdns_by_ip: Dict[str, str] = {}
            if self.config.enable_mdns_enrich:
                mdns_by_ip = await mdns_enrich()

            now = time.time()
            known: Dict[str, KnownDeviceRecord] = dict(self.config.known_devices)
            events: List[tuple] = []

            for mac, neigh in by_mac.items():
                existing = known.get(mac)
                if existing is None:
                    if mac in self_macs:
                        hostname = self_macs[mac]
                    else:
                        hostname = await reverse_dns(neigh.ip) if self.config.enable_rdns else None
                    vendor = oui.lookup_vendor(mac) if self.config.enable_vendor_lookup else None
                    record = KnownDeviceRecord(
                        mac=mac, last_ip=neigh.ip, interface=neigh.interface,
                        first_seen=now, last_seen=now, online=True, missed_scans=0,
                        hostname=hostname, vendor=vendor, mdns_name=mdns_by_ip.get(neigh.ip),
                    )
                    known[mac] = record
                    events.append(("new", mac))
                    continue

                record = existing.model_copy()
                was_offline = not record.online
                record.last_ip = neigh.ip
                record.interface = neigh.interface
                record.last_seen = now
                record.missed_scans = 0
                record.online = True
                record.offline_since = None
                if not record.hostname:
                    if mac in self_macs:
                        record.hostname = self_macs[mac]
                    elif self.config.enable_rdns:
                        record.hostname = await reverse_dns(neigh.ip)
                if not record.vendor and self.config.enable_vendor_lookup:
                    record.vendor = oui.lookup_vendor(mac)
                mdns_name = mdns_by_ip.get(neigh.ip)
                if mdns_name:
                    record.mdns_name = mdns_name
                known[mac] = record
                if was_offline:
                    events.append(("online", mac))

            seen_macs = set(by_mac.keys())
            for mac, record in list(known.items()):
                if mac in seen_macs or not record.online:
                    continue
                record = record.model_copy()
                record.missed_scans += 1
                if record.missed_scans >= max(1, self.config.offline_grace_cycles):
                    record.online = False
                    record.offline_since = now
                    events.append(("offline", mac))
                known[mac] = record

            # update_config's bus round-trip needs plain JSON-able data —
            # publishing raw KnownDeviceRecord instances silently never
            # reaches the backend (json.dumps rejects them; see
            # rlx_bus.client._send), so the scanned roster looked "saved"
            # (self.config updates locally either way) but never actually
            # persisted to the proxy's yml and was lost on every restart.
            await self.update_config({"known_devices": {m: r.model_dump() for m, r in known.items()}})
            self._last_scan_at = now
            await self.publish_state()
            for event, mac in events:
                await self._fire_event(event, mac, known[mac])
            return self._snapshot()
        finally:
            self._scanning = False

    # ─── merge/state ─────────────────────────────────────────────────
    def _merge(self, mac: str, record: KnownDeviceRecord) -> Dict[str, Any]:
        label = self.config.labels.get(mac)
        entry = DeviceEntry(
            **record.model_dump(),
            label=label.label if label else None,
            notes=label.notes if label else None,
            device_type=label.device_type if label else None,
            ignored=bool(label.ignored) if label else False,
            trusted=bool(label.trusted) if label else False,
            display_name=(
                (label.label if label and label.label else None)
                or record.hostname or record.mdns_name or record.vendor or mac
            ),
        )
        return entry.model_dump()

    def _snapshot(self) -> Dict[str, Any]:
        devices = [self._merge(mac, r) for mac, r in self.config.known_devices.items()]
        devices.sort(key=lambda d: (not d["online"], d["display_name"] or ""))
        online = sum(1 for d in devices if d["online"])
        state = NetworkScannerState(
            devices=devices,
            total=len(devices),
            online=online,
            offline=len(devices) - online,
            subnets=[s.cidr for s in ifaces.local_subnets()],
            scan_interval_s=self.config.scan_interval_s,
            last_scan=self._last_scan_at,
            alert_target=self.config.alert_target,
            alert_on=list(self.config.alert_on),
        )
        return state.model_dump()

    async def publish_state(self) -> None:
        await self.publish("state", self._snapshot(), retained=True)

    async def _fire_event(self, event: str, mac: str, record: KnownDeviceRecord) -> None:
        merged = self._merge(mac, record)
        await self.publish(f"device_{event}", merged)
        if event not in self.config.alert_on or not self.config.alert_target:
            return
        name = merged.get("display_name") or mac
        ip = record.last_ip or "an unknown address"
        if event == "new":
            text = f"New device joined the network: {name} ({ip})"
        elif event == "offline":
            text = f"Device went offline: {name} (last seen at {ip})"
        else:
            text = f"Device back online: {name} ({ip})"
        await self.publish(self.config.alert_target, {"action": "speak", "text": text})

    # ─── scanning controls ───────────────────────────────────────────
    @service_method("scan_now", publishes=["state"])
    async def m_scan_now(self) -> Dict[str, Any]:
        return await self._scan_once()

    @service_method("set_scan_interval", publishes=["state"])
    async def m_set_scan_interval(self, seconds: float) -> Dict[str, Any]:
        await self.update_config({"scan_interval_s": max(5, int(seconds))})
        await self.publish_state()
        return {"scan_interval_s": self.config.scan_interval_s}

    @service_method("set_offline_grace", publishes=["state"])
    async def m_set_offline_grace(self, cycles: int) -> Dict[str, Any]:
        await self.update_config({"offline_grace_cycles": max(1, int(cycles))})
        await self.publish_state()
        return {"offline_grace_cycles": self.config.offline_grace_cycles}

    @service_method("set_subnets", publishes=["state"])
    async def m_set_subnets(self, subnets: List[str]) -> Dict[str, Any]:
        valid = [s for s in subnets if ifaces.parse_subnet_override(s) is not None]
        await self.update_config({"subnets": valid})
        await self.publish_state()
        return {"subnets": valid}

    @service_method("set_alert_target", publishes=["state"])
    async def m_set_alert_target(self, topic: Optional[str] = None) -> Dict[str, Any]:
        await self.update_config({"alert_target": topic or None})
        await self.publish_state()
        return {"alert_target": self.config.alert_target}

    @service_method("set_alert_on", publishes=["state"])
    async def m_set_alert_on(self, events: List[str]) -> Dict[str, Any]:
        valid = [e for e in events if e in _VALID_EVENTS]
        await self.update_config({"alert_on": valid})
        await self.publish_state()
        return {"alert_on": valid}

    # ─── labeling ─────────────────────────────────────────────────────
    @service_method("set_label", publishes=["state"])
    async def m_set_label(
        self, mac: str, label: Optional[str] = None, notes: Optional[str] = None,
        device_type: Optional[str] = None, ignored: Optional[bool] = None,
        trusted: Optional[bool] = None,
    ) -> Dict[str, Any]:
        mac = mac.lower()
        existing = self.config.labels.get(mac)
        updated = UserLabel(
            mac=mac,
            label=label if label is not None else (existing.label if existing else None),
            notes=notes if notes is not None else (existing.notes if existing else None),
            device_type=device_type if device_type is not None else (existing.device_type if existing else None),
            ignored=bool(ignored) if ignored is not None else (existing.ignored if existing else False),
            trusted=bool(trusted) if trusted is not None else (existing.trusted if existing else False),
            updated_at=time.time(),
        )
        labels = dict(self.config.labels)
        labels[mac] = updated
        # See _scan_once's comment — update_config needs plain dicts, not
        # live UserLabel instances, or the patch silently never persists.
        await self.update_config({"labels": {m: l.model_dump() for m, l in labels.items()}})
        await self.publish_state()
        return {"mac": mac, **updated.model_dump()}

    @service_method("remove_device", publishes=["state"])
    async def m_remove_device(self, mac: str) -> Dict[str, Any]:
        mac = mac.lower()
        known = dict(self.config.known_devices)
        labels = dict(self.config.labels)
        had_known = known.pop(mac, None) is not None
        had_label = labels.pop(mac, None) is not None
        await self.update_config({
            "known_devices": {m: r.model_dump() for m, r in known.items()},
            "labels": {m: l.model_dump() for m, l in labels.items()},
        })
        await self.publish_state()
        return {"removed": mac, "found": had_known or had_label}

    # ─── device_directory capability ───────────────────────────────────
    @service_method("list_devices")
    async def m_list_devices(self, include_ignored: bool = False) -> Dict[str, Any]:
        devices = [self._merge(mac, r) for mac, r in self.config.known_devices.items()]
        if not include_ignored:
            devices = [d for d in devices if not d["ignored"]]
        devices.sort(key=lambda d: (not d["online"], d["display_name"] or ""))
        return {"devices": devices}

    @service_method("resolve")
    async def m_resolve(self, query: str) -> Dict[str, Any]:
        q = (query or "").strip().lower()
        if not q:
            return {"device": None}
        merged = [self._merge(mac, r) for mac, r in self.config.known_devices.items()]
        for d in merged:
            exact_fields = (d.get("label"), d.get("hostname"), d.get("mdns_name"), d.get("last_ip"), d.get("mac"))
            if any(f and str(f).lower() == q for f in exact_fields):
                return {"device": d}
        for d in merged:
            loose_fields = (d.get("label"), d.get("hostname"), d.get("mdns_name"))
            if any(f and q in str(f).lower() for f in loose_fields):
                return {"device": d}
        return {"device": None}

    @service_method("get_device")
    async def m_get_device(self, id: str) -> Dict[str, Any]:
        mac = (id or "").lower()
        record = self.config.known_devices.get(mac)
        if record is None:
            return {"device": None}
        return {"device": self._merge(mac, record)}
