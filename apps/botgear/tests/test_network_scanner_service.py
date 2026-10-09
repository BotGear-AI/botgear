# unmanaged
"""Unit tests for the network_scanner subprocess service.

Covers ifaces.py (interface filtering, mirroring test_mdns.py's
_all_lan_addresses coverage), scan.py (neighbor-table line parsing,
MAC dedup, host-list truncation), oui.py (vendor lookup), and
NetworkScannerService's scan-cycle state machine (new/online/offline
debounce, alert firing) plus its @service_method actions.

The service lives in its own subprocess venv with its own pyproject.
We add its src/ to sys.path so pytest can import it from the top-level
botgear venv, mirroring test_tasmota_service.py / test_arduino_service.py.
"""
from __future__ import annotations

import asyncio
import ipaddress
import sys
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

_SRC = Path(__file__).resolve().parents[1] / "repo" / "network_scanner" / "1.0.0" / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ─────────────────────────────────────────────────────────────────────
# ifaces.py — subnet enumeration / interface filtering
# ─────────────────────────────────────────────────────────────────────


class _FakeIp:
    def __init__(self, ip: str, prefix: int) -> None:
        self.ip = ip
        self.is_IPv4 = True
        self.network_prefix = prefix


class _FakeAdapter:
    def __init__(self, name: str, ips) -> None:
        self.name = name
        self.nice_name = name
        self.ips = [_FakeIp(ip, prefix) for ip, prefix in ips]


def test_local_subnets_excludes_loopback_and_virtual_bridges(monkeypatch):
    from network_scanner_service import ifaces

    fake_adapters = [
        _FakeAdapter("lo", [("127.0.0.1", 8)]),
        _FakeAdapter("docker0", [("172.17.0.1", 16)]),
        _FakeAdapter("br-89ab00112233", [("172.20.0.1", 16)]),
        _FakeAdapter("veth3f2a1b9", [("169.254.1.2", 16)]),
        _FakeAdapter("virbr0", [("192.168.122.1", 24)]),
        _FakeAdapter("wlo1", [("192.168.0.157", 24)]),
    ]
    monkeypatch.setattr(ifaces.ifaddr, "get_adapters", lambda: fake_adapters)

    subnets = ifaces.local_subnets()

    assert len(subnets) == 1
    assert subnets[0].interface == "wlo1"
    assert subnets[0].ip == "192.168.0.157"
    assert subnets[0].cidr == "192.168.0.0/24"


def test_local_subnets_empty_when_nothing_real_is_up(monkeypatch):
    from network_scanner_service import ifaces

    monkeypatch.setattr(ifaces.ifaddr, "get_adapters", lambda: [_FakeAdapter("lo", [("127.0.0.1", 8)])])
    assert ifaces.local_subnets() == []


def test_parse_subnet_override_valid_and_invalid():
    from network_scanner_service import ifaces

    net = ifaces.parse_subnet_override("192.168.1.0/24")
    assert net is not None and str(net) == "192.168.1.0/24"
    assert ifaces.parse_subnet_override("not-a-subnet") is None


# ─────────────────────────────────────────────────────────────────────
# scan.py — neighbor-table parsing / dedup / host enumeration
# ─────────────────────────────────────────────────────────────────────


def test_parse_neigh_output_reads_reachable_and_stale_and_failed_lines():
    from network_scanner_service.scan import _parse_neigh_output

    text = (
        "192.168.1.1 dev eth0 lladdr aa:bb:cc:dd:ee:ff REACHABLE\n"
        "192.168.1.50 dev eth0 lladdr 11:22:33:44:55:66 STALE\n"
        "192.168.1.99 dev eth0  FAILED\n"
        "\n"
    )
    entries = _parse_neigh_output(text)
    assert [(e.ip, e.mac, e.state) for e in entries] == [
        ("192.168.1.1", "aa:bb:cc:dd:ee:ff", "REACHABLE"),
        ("192.168.1.50", "11:22:33:44:55:66", "STALE"),
        ("192.168.1.99", None, "FAILED"),
    ]
    assert entries[0].reachable is True
    assert entries[1].reachable is True
    assert entries[2].reachable is False  # no mac captured


def test_merge_by_mac_dedups_and_skips_macless_entries():
    from network_scanner_service.scan import NeighborEntry, merge_by_mac

    entries = [
        NeighborEntry(ip="10.0.0.5", mac=None, interface="eth0", state="FAILED"),
        NeighborEntry(ip="10.0.0.9", mac="aa:bb:cc:dd:ee:ff", interface="eth0", state="STALE"),
        NeighborEntry(ip="10.0.0.9", mac="aa:bb:cc:dd:ee:ff", interface="eth0", state="REACHABLE"),
    ]
    by_mac = merge_by_mac(entries)
    assert list(by_mac.keys()) == ["aa:bb:cc:dd:ee:ff"]
    assert by_mac["aa:bb:cc:dd:ee:ff"].ip == "10.0.0.9"


def test_hosts_for_network_truncates_to_max_hosts():
    from network_scanner_service.scan import hosts_for_network

    net = ipaddress.ip_network("10.0.0.0/29")  # 6 usable hosts
    assert len(hosts_for_network(net, 3)) == 3
    assert len(hosts_for_network(net, 100)) == 6


# ─────────────────────────────────────────────────────────────────────
# oui.py — vendor lookup
# ─────────────────────────────────────────────────────────────────────


def test_lookup_vendor_known_prefix_case_and_separator_insensitive():
    from network_scanner_service.oui import lookup_vendor

    assert lookup_vendor("b8:27:eb:11:22:33") == "Raspberry Pi Foundation"
    assert lookup_vendor("B8-27-EB-11-22-33") == "Raspberry Pi Foundation"


def test_lookup_vendor_unknown_prefix_returns_none():
    from network_scanner_service.oui import lookup_vendor

    assert lookup_vendor("00:00:00:11:22:33") is None
    assert lookup_vendor("") is None


# ─────────────────────────────────────────────────────────────────────
# NetworkScannerService — scan cycle + service_method actions
# ─────────────────────────────────────────────────────────────────────


class _FakeBus:
    """Minimal BusClient stand-in — mirrors test_tasmota_service.py's."""

    def __init__(self) -> None:
        self.published: List[Dict[str, Any]] = []

    async def publish(self, topic: str, payload: Any, *, retained: bool = False) -> None:
        self.published.append({"topic": topic, "payload": payload, "retained": retained})

    async def subscribe(self, topic: str, handler):
        pass

    def all(self, suffix: str) -> List[Dict[str, Any]]:
        return [m for m in self.published if m["topic"].endswith(suffix)]

    def last(self, suffix: str) -> Dict[str, Any]:
        matches = self.all(suffix)
        assert matches, f"no publish to a topic ending in {suffix!r}"
        return matches[-1]


@pytest.fixture
def service(monkeypatch):
    from network_scanner_service.service import NetworkScannerService

    svc = NetworkScannerService("network_scanner-test", _FakeBus())

    # Real update_config re-validates through merge_dict (a full Pydantic
    # reconstruction), which is what coerces a plain dict patch — the only
    # kind that survives the real bus's json.dumps — back into
    # KnownDeviceRecord/UserLabel instances. A naive setattr-per-key mock
    # skips that step, so a service accidentally publishing live model
    # instances (which silently never reach the backend for real — see
    # rlx_bus.client._send) would still pass every test here.
    async def _record_update(patch):
        svc.config = svc.config.merge_dict(patch)
    monkeypatch.setattr(svc, "update_config", _record_update)
    return svc


def _neighbor(ip: str, mac: str, iface: str = "eth0", state: str = "REACHABLE"):
    from network_scanner_service.scan import NeighborEntry
    return NeighborEntry(ip=ip, mac=mac, interface=iface, state=state)


def _patch_scan_cycle(monkeypatch, neighbors, mdns_map=None, hostname=None, local_subnets=()):
    import network_scanner_service.service as svc_mod

    monkeypatch.setattr(svc_mod, "prime_arp_cache", AsyncMock(return_value=None))
    monkeypatch.setattr(svc_mod, "read_neighbor_table", AsyncMock(return_value=neighbors))
    monkeypatch.setattr(svc_mod, "mdns_enrich", AsyncMock(return_value=mdns_map or {}))
    monkeypatch.setattr(svc_mod, "reverse_dns", AsyncMock(return_value=hostname))
    # Without this, tests would hit the REAL machine's real interfaces
    # (self-discovery below queries ifaces.local_subnets() directly, not
    # just via _target_networks()) — nondeterministic across dev boxes
    # and CI runners alike. Empty by default; tests that care about
    # self-discovery pass their own fake Subnet list.
    monkeypatch.setattr(svc_mod.ifaces, "local_subnets", lambda: list(local_subnets))


@pytest.mark.asyncio
async def test_scan_once_discovers_new_device_and_fires_new_alert(service, monkeypatch):
    monkeypatch.setattr(service, "_target_networks", lambda: [ipaddress.ip_network("10.0.0.0/30")])
    _patch_scan_cycle(monkeypatch, [_neighbor("10.0.0.1", "aa:bb:cc:dd:ee:ff")], hostname="thing.lan")
    service.config.alert_target = "/chat/chat-1/control"

    result = await service._scan_once()

    assert "aa:bb:cc:dd:ee:ff" in service.config.known_devices
    record = service.config.known_devices["aa:bb:cc:dd:ee:ff"]
    assert record.online is True
    assert record.last_ip == "10.0.0.1"
    assert record.hostname == "thing.lan"
    assert result["total"] == 1
    assert result["online"] == 1

    new_event = service.bus.last("device_new")
    assert new_event["payload"]["mac"] == "aa:bb:cc:dd:ee:ff"

    alert = service.bus.last("/chat/chat-1/control")
    assert alert["payload"]["action"] == "speak"
    assert "New device joined" in alert["payload"]["text"]


@pytest.mark.asyncio
async def test_scan_once_does_not_alert_when_event_not_in_alert_on(service, monkeypatch):
    monkeypatch.setattr(service, "_target_networks", lambda: [ipaddress.ip_network("10.0.0.0/30")])
    _patch_scan_cycle(monkeypatch, [_neighbor("10.0.0.1", "aa:bb:cc:dd:ee:ff")])
    service.config.alert_target = "/chat/chat-1/control"
    service.config.alert_on = ["offline"]  # "new" excluded

    await service._scan_once()

    assert service.bus.all("/chat/chat-1/control") == []


@pytest.mark.asyncio
async def test_scan_once_reseen_device_resets_missed_scans_without_event(service, monkeypatch):
    now = time.time()
    from network_scanner_service.service import KnownDeviceRecord
    service.config.known_devices = {
        "aa:bb:cc:dd:ee:ff": KnownDeviceRecord(
            mac="aa:bb:cc:dd:ee:ff", last_ip="10.0.0.1", first_seen=now - 100,
            last_seen=now - 60, online=True, missed_scans=1,
        ),
    }
    monkeypatch.setattr(service, "_target_networks", lambda: [ipaddress.ip_network("10.0.0.0/30")])
    _patch_scan_cycle(monkeypatch, [_neighbor("10.0.0.1", "aa:bb:cc:dd:ee:ff")])

    await service._scan_once()

    record = service.config.known_devices["aa:bb:cc:dd:ee:ff"]
    assert record.missed_scans == 0
    assert record.online is True
    assert service.bus.all("device_online") == []  # was already online — no flip


@pytest.mark.asyncio
async def test_scan_once_flips_offline_after_grace_cycles(service, monkeypatch):
    from network_scanner_service.service import KnownDeviceRecord
    now = time.time()
    service.config.offline_grace_cycles = 3
    service.config.known_devices = {
        "aa:bb:cc:dd:ee:ff": KnownDeviceRecord(
            mac="aa:bb:cc:dd:ee:ff", last_ip="10.0.0.1", first_seen=now - 500,
            last_seen=now - 200, online=True, missed_scans=2,
        ),
    }
    monkeypatch.setattr(service, "_target_networks", lambda: [ipaddress.ip_network("10.0.0.0/30")])
    _patch_scan_cycle(monkeypatch, [])  # nothing seen this cycle
    service.config.alert_target = "/chat/chat-1/control"

    await service._scan_once()

    record = service.config.known_devices["aa:bb:cc:dd:ee:ff"]
    assert record.missed_scans == 3
    assert record.online is False
    assert record.offline_since is not None

    offline_event = service.bus.last("device_offline")
    assert offline_event["payload"]["mac"] == "aa:bb:cc:dd:ee:ff"
    alert = service.bus.last("/chat/chat-1/control")
    assert "went offline" in alert["payload"]["text"]


@pytest.mark.asyncio
async def test_scan_once_single_miss_does_not_flip_offline(service, monkeypatch):
    from network_scanner_service.service import KnownDeviceRecord
    now = time.time()
    service.config.offline_grace_cycles = 3
    service.config.known_devices = {
        "aa:bb:cc:dd:ee:ff": KnownDeviceRecord(
            mac="aa:bb:cc:dd:ee:ff", last_ip="10.0.0.1", first_seen=now - 500,
            last_seen=now - 60, online=True, missed_scans=0,
        ),
    }
    monkeypatch.setattr(service, "_target_networks", lambda: [ipaddress.ip_network("10.0.0.0/30")])
    _patch_scan_cycle(monkeypatch, [])

    await service._scan_once()

    record = service.config.known_devices["aa:bb:cc:dd:ee:ff"]
    assert record.missed_scans == 1
    assert record.online is True
    assert service.bus.all("device_offline") == []


@pytest.mark.asyncio
async def test_scan_once_device_coming_back_fires_online_event(service, monkeypatch):
    from network_scanner_service.service import KnownDeviceRecord
    now = time.time()
    service.config.known_devices = {
        "aa:bb:cc:dd:ee:ff": KnownDeviceRecord(
            mac="aa:bb:cc:dd:ee:ff", last_ip="10.0.0.1", first_seen=now - 500,
            last_seen=now - 400, online=False, missed_scans=5, offline_since=now - 300,
        ),
    }
    monkeypatch.setattr(service, "_target_networks", lambda: [ipaddress.ip_network("10.0.0.0/30")])
    _patch_scan_cycle(monkeypatch, [_neighbor("10.0.0.1", "aa:bb:cc:dd:ee:ff")])

    await service._scan_once()

    record = service.config.known_devices["aa:bb:cc:dd:ee:ff"]
    assert record.online is True
    assert record.offline_since is None
    online_event = service.bus.last("device_online")
    assert online_event["payload"]["mac"] == "aa:bb:cc:dd:ee:ff"


@pytest.mark.asyncio
async def test_scan_once_adds_this_box_as_a_device(service, monkeypatch):
    """ip neigh show never contains this box's own address (the kernel
    doesn't ARP itself) — without synthesizing an entry from the local
    interface's own MAC, the scanner's own host never appears in its
    own roster no matter how many cycles run."""
    from network_scanner_service.ifaces import Subnet

    monkeypatch.setattr(service, "_target_networks", lambda: [ipaddress.ip_network("10.0.0.0/30")])
    self_subnet = Subnet(
        interface="wlo1", ip="10.0.0.5", netmask="255.255.255.0",
        network=ipaddress.ip_network("10.0.0.0/24"), mac="d8:f2:ca:98:51:2a",
    )
    _patch_scan_cycle(monkeypatch, [], local_subnets=[self_subnet])
    monkeypatch.setattr("socket.gethostname", lambda: "worke")

    result = await service._scan_once()

    assert "d8:f2:ca:98:51:2a" in service.config.known_devices
    record = service.config.known_devices["d8:f2:ca:98:51:2a"]
    assert record.last_ip == "10.0.0.5"
    assert record.hostname == "worke"
    assert result["total"] == 1


@pytest.mark.asyncio
async def test_scan_once_self_hostname_backfills_even_if_rdns_previously_failed(service, monkeypatch):
    """A pre-existing self record from before this fix (or one whose
    rDNS attempt simply failed, same as any other device) should get
    its hostname corrected on the next cycle rather than staying blank
    forever — hostname is only ever filled in once devices are new."""
    from network_scanner_service.ifaces import Subnet
    from network_scanner_service.service import KnownDeviceRecord

    now = time.time()
    service.config.known_devices = {
        "d8:f2:ca:98:51:2a": KnownDeviceRecord(
            mac="d8:f2:ca:98:51:2a", last_ip="10.0.0.5", first_seen=now - 500,
            last_seen=now - 400, online=True, hostname=None,
        ),
    }
    monkeypatch.setattr(service, "_target_networks", lambda: [ipaddress.ip_network("10.0.0.0/30")])
    self_subnet = Subnet(
        interface="wlo1", ip="10.0.0.5", netmask="255.255.255.0",
        network=ipaddress.ip_network("10.0.0.0/24"), mac="d8:f2:ca:98:51:2a",
    )
    _patch_scan_cycle(monkeypatch, [], local_subnets=[self_subnet])
    monkeypatch.setattr("socket.gethostname", lambda: "worke")

    await service._scan_once()

    assert service.config.known_devices["d8:f2:ca:98:51:2a"].hostname == "worke"


@pytest.mark.asyncio
async def test_set_label_creates_then_updates_without_clobbering_other_fields(service):
    await service.m_set_label(mac="AA:BB:CC:DD:EE:FF", label="the dishwasher")
    label = service.config.labels["aa:bb:cc:dd:ee:ff"]
    assert label.label == "the dishwasher"
    assert label.ignored is False

    await service.m_set_label(mac="aa:bb:cc:dd:ee:ff", ignored=True)
    label = service.config.labels["aa:bb:cc:dd:ee:ff"]
    assert label.label == "the dishwasher"  # untouched by the second call
    assert label.ignored is True


@pytest.mark.asyncio
async def test_remove_device_clears_both_known_and_label(service):
    from network_scanner_service.service import KnownDeviceRecord
    now = time.time()
    mac = "aa:bb:cc:dd:ee:ff"
    service.config.known_devices = {mac: KnownDeviceRecord(mac=mac, first_seen=now, last_seen=now)}
    await service.m_set_label(mac=mac, label="thing")

    result = await service.m_remove_device(mac=mac)

    assert result["found"] is True
    assert mac not in service.config.known_devices
    assert mac not in service.config.labels


@pytest.mark.asyncio
async def test_list_devices_excludes_ignored_by_default(service):
    from network_scanner_service.service import KnownDeviceRecord
    now = time.time()
    service.config.known_devices = {
        "aa:aa:aa:aa:aa:aa": KnownDeviceRecord(mac="aa:aa:aa:aa:aa:aa", first_seen=now, last_seen=now),
        "bb:bb:bb:bb:bb:bb": KnownDeviceRecord(mac="bb:bb:bb:bb:bb:bb", first_seen=now, last_seen=now),
    }
    await service.m_set_label(mac="bb:bb:bb:bb:bb:bb", ignored=True)

    visible = await service.m_list_devices()
    assert [d["mac"] for d in visible["devices"]] == ["aa:aa:aa:aa:aa:aa"]

    everything = await service.m_list_devices(include_ignored=True)
    assert len(everything["devices"]) == 2


@pytest.mark.asyncio
async def test_resolve_matches_label_case_insensitively(service):
    from network_scanner_service.service import KnownDeviceRecord
    now = time.time()
    mac = "aa:bb:cc:dd:ee:ff"
    service.config.known_devices = {mac: KnownDeviceRecord(mac=mac, last_ip="192.168.0.38", first_seen=now, last_seen=now)}
    await service.m_set_label(mac=mac, label="The Dishwasher")

    result = await service.m_resolve(query="the dishwasher")
    assert result["device"]["mac"] == mac

    result = await service.m_resolve(query="192.168.0.38")
    assert result["device"]["mac"] == mac

    assert (await service.m_resolve(query="nonexistent"))["device"] is None


@pytest.mark.asyncio
async def test_get_device_unknown_mac_returns_none(service):
    result = await service.m_get_device(id="00:00:00:00:00:00")
    assert result["device"] is None
