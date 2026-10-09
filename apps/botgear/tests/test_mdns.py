# unmanaged
"""Tests for runtime/mdns.py — auto-discovery wiring.

Doesn't hit real multicast — the ``start()`` API takes optional
``zc_factory`` + ``browser_factory`` injection points so we can drive
the ServiceBrowser callback with mock state changes and assert the
right peer_manager.connect calls happen.
"""
from __future__ import annotations

from typing import List
from unittest.mock import MagicMock

import pytest

from botgear.runtime import mdns


# Real ServiceStateChange has Added / Updated / Removed.
class _FakeStateChange:
    Added = "added"
    Updated = "updated"
    Removed = "removed"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Each test starts with mdns module state cleared. We stub the
    zeroconf module so the local imports inside mdns.start /
    _on_service_change find ServiceInfo + ServiceStateChange without
    touching real multicast.

    ServiceInfo is a passthrough (the test doesn't care about its
    internals); ServiceStateChange uses our fake enum stand-in.
    """
    mdns.reset_for_tests()
    fake_module = type("_FakeZeroconfModule", (), {
        "ServiceStateChange": _FakeStateChange,
        "ServiceInfo": MagicMock(),
    })
    monkeypatch.setitem(__import__("sys").modules, "zeroconf", fake_module)
    yield
    mdns.reset_for_tests()


def _fake_zc_factory():
    """Constructs a MagicMock that mimics Zeroconf enough for start()
    to register + browse without crashing."""
    mock = MagicMock()
    # get_service_info: tests override per-case.
    mock.get_service_info = MagicMock(return_value=None)
    return mock


def _fake_browser_factory(callback_holder):
    """Build a browser_factory that captures the callback so the test
    can invoke it directly."""
    def factory(zc, service_type, handler):
        callback_holder.append(handler)
        return MagicMock()
    return factory


# ─────────────────────────────────────────────────────────────────────
# _all_lan_addresses — interface filtering
# ─────────────────────────────────────────────────────────────────────


class _FakeIp:
    def __init__(self, ip: str) -> None:
        self.ip = ip
        self.is_IPv4 = True


class _FakeAdapter:
    def __init__(self, name: str, ips):
        self.name = name
        self.ips = [_FakeIp(ip) for ip in ips]


def test_all_lan_addresses_excludes_loopback_and_virtual_bridges(monkeypatch):
    """Reproduces the field bug: a Docker host announced docker0's
    172.17.0.1 alongside its real LAN IP, and a remote peer's mDNS
    resolver returned the Docker address for our SRV hostname — a
    machine that isn't us, that can never route to it, hangs in a
    silent TCP-connect timeout. Only the real, routable LAN address
    should survive."""
    import socket as socket_mod
    fake_adapters = [
        _FakeAdapter("lo", ["127.0.0.1"]),
        _FakeAdapter("docker0", ["172.17.0.1"]),
        _FakeAdapter("br-89ab00112233", ["172.20.0.1"]),
        _FakeAdapter("veth3f2a1b9", ["169.254.1.2"]),
        _FakeAdapter("virbr0", ["192.168.122.1"]),
        _FakeAdapter("wlo1", ["192.168.0.157"]),
    ]
    monkeypatch.setattr(
        "botgear.runtime.mdns.ifaddr.get_adapters", lambda: fake_adapters,
    )
    addrs = mdns._all_lan_addresses()
    assert addrs == [socket_mod.inet_aton("192.168.0.157")]


def test_all_lan_addresses_falls_back_to_loopback_when_nothing_else(monkeypatch):
    """No real interface up at all (e.g. mdns.start() racing DHCP at
    boot) — better to announce loopback than crash zeroconf with an
    empty address list."""
    import socket as socket_mod
    monkeypatch.setattr(
        "botgear.runtime.mdns.ifaddr.get_adapters",
        lambda: [_FakeAdapter("lo", ["127.0.0.1"])],
    )
    addrs = mdns._all_lan_addresses()
    assert addrs == [socket_mod.inet_aton("127.0.0.1")]


# ─────────────────────────────────────────────────────────────────────
# start / stop
# ─────────────────────────────────────────────────────────────────────


def test_start_registers_and_browses(monkeypatch):
    cb_holder: List = []
    fake_zc = _fake_zc_factory()
    mdns.start(
        runtime_id="witty-gizmo", port=8998,
        zc_factory=lambda: fake_zc,
        browser_factory=_fake_browser_factory(cb_holder),
    )
    assert mdns.is_started()
    # register_service was called
    fake_zc.register_service.assert_called_once()
    # Browser callback was wired
    assert len(cb_holder) == 1


def test_start_is_idempotent(monkeypatch):
    cb_holder: List = []
    fake_zc = _fake_zc_factory()
    mdns.start(
        runtime_id="witty-gizmo", port=8998,
        zc_factory=lambda: fake_zc,
        browser_factory=_fake_browser_factory(cb_holder),
    )
    mdns.start(
        runtime_id="witty-gizmo", port=8998,
        zc_factory=lambda: fake_zc,
        browser_factory=_fake_browser_factory(cb_holder),
    )
    # register_service still called only once
    assert fake_zc.register_service.call_count == 1


def test_rescan_noop_when_never_started():
    """Nothing to rebuild if start() was never called."""
    assert mdns.rescan() is False


def test_rescan_rebuilds_with_same_identity(monkeypatch):
    """The fix for the boot race (Wi-Fi comes up after mdns.start()
    already snapshotted zero usable interfaces): rescan() tears down
    the stale zeroconf instance and builds a fresh one with the same
    runtime_id/port, so a freshly-constructed Zeroconf() gets a chance
    to re-enumerate interfaces. Unlike start(), rescan() doesn't take
    zc_factory/browser_factory (those are test-only on start()) — it
    always goes through the real ``from zeroconf import ...`` names, so
    this test stubs those directly on the fake module instead."""
    zc_instances: List[MagicMock] = []

    def _zc_factory():
        m = MagicMock()
        m.get_service_info = MagicMock(return_value=None)
        zc_instances.append(m)
        return m

    browsers: List[MagicMock] = []

    def _service_browser(zc, service_type, handlers):
        b = MagicMock()
        browsers.append(b)
        return b

    fake_module = type("_FakeZeroconfModule", (), {
        "ServiceStateChange": _FakeStateChange,
        "ServiceInfo": MagicMock(),
        "Zeroconf": _zc_factory,
        "ServiceBrowser": _service_browser,
    })
    monkeypatch.setitem(__import__("sys").modules, "zeroconf", fake_module)

    mdns.start(runtime_id="witty-gizmo", port=8998)
    assert len(zc_instances) == 1
    first_zc = zc_instances[0]

    assert mdns.rescan() is True

    first_zc.unregister_all_services.assert_called_once()
    first_zc.close.assert_called_once()
    assert len(zc_instances) == 2
    assert len(browsers) == 2
    assert mdns.is_started()


def test_stop_unregisters_and_closes(monkeypatch):
    cb_holder: List = []
    fake_zc = _fake_zc_factory()
    mdns.start(
        runtime_id="witty-gizmo", port=8998,
        zc_factory=lambda: fake_zc,
        browser_factory=_fake_browser_factory(cb_holder),
    )
    mdns.stop()
    fake_zc.unregister_all_services.assert_called_once()
    fake_zc.close.assert_called_once()
    assert not mdns.is_started()


# ─────────────────────────────────────────────────────────────────────
# Discovery callback — _on_service_change
# ─────────────────────────────────────────────────────────────────────


def _fire_added(zc, name: str, peer_id: str, host_v4: str, port: int) -> None:
    """Helper: simulate a zeroconf Added event by populating
    get_service_info + invoking the module callback directly."""
    import socket
    info = MagicMock()
    info.properties = {b"id": peer_id.encode("utf-8"),
                       b"version": b"1.0.0"}
    info.addresses = [socket.inet_aton(host_v4)]
    info.port = port
    # No SRV hostname → the code falls back to the IPv4 address. Must be
    # set explicitly: a bare MagicMock returns a truthy auto-attribute
    # for ``.server``, which the hostname-preference path would otherwise
    # splice into the URL instead of host_v4.
    info.server = None
    zc.get_service_info.return_value = info
    mdns._on_service_change(zc, mdns._SERVICE_TYPE, name, _FakeStateChange.Added)


def test_discovery_auto_connects_to_remote_peer(monkeypatch):
    """Discovery must hop onto the bound asyncio loop because the
    zeroconf browser thread can't call asyncio.create_task() directly.
    Our fake loop captures the call_soon_threadsafe scheduling and
    immediately fires the inner _connect_on_loop helper."""
    cb_holder: List = []
    fake_zc = _fake_zc_factory()
    connect_calls: List[str] = []
    monkeypatch.setattr(
        "botgear.runtime.peer_manager.connect",
        lambda url: connect_calls.append(url) or MagicMock(),
    )

    # Fake loop — its call_soon_threadsafe(fn, *args) calls fn(*args)
    # synchronously. Good enough to verify the hop happens.
    class _FakeLoop:
        def call_soon_threadsafe(self, fn, *args):
            fn(*args)
    monkeypatch.setattr(
        "botgear.runtime.peer_manager._manager_loop", _FakeLoop(),
    )

    mdns.start(
        runtime_id="witty-gizmo", port=8998,
        zc_factory=lambda: fake_zc,
        browser_factory=_fake_browser_factory(cb_holder),
        auto_connect=True,
    )
    _fire_added(fake_zc, "silly-droid._botgear._tcp.local.",
                peer_id="silly-droid", host_v4="10.0.0.5", port=8998)
    assert connect_calls == ["ws://10.0.0.5:8998/v1/ws"]


def test_discovery_skips_self(monkeypatch):
    """The browser sees our own announcement too — we filter by
    runtime_id to avoid auto-dialing ourselves."""
    cb_holder: List = []
    fake_zc = _fake_zc_factory()
    connect_calls: List[str] = []
    monkeypatch.setattr(
        "botgear.runtime.peer_manager.connect",
        lambda url: connect_calls.append(url) or MagicMock(),
    )
    mdns.start(
        runtime_id="witty-gizmo", port=8998,
        zc_factory=lambda: fake_zc,
        browser_factory=_fake_browser_factory(cb_holder),
    )
    _fire_added(fake_zc, "witty-gizmo._botgear._tcp.local.",
                peer_id="witty-gizmo", host_v4="127.0.0.1", port=8998)
    assert connect_calls == []


def test_discovery_respects_auto_connect_false(monkeypatch):
    """With auto_connect off, discovered peers are LOGGED but not
    dialed — user must connect manually from the Topology page."""
    cb_holder: List = []
    fake_zc = _fake_zc_factory()
    connect_calls: List[str] = []
    monkeypatch.setattr(
        "botgear.runtime.peer_manager.connect",
        lambda url: connect_calls.append(url) or MagicMock(),
    )
    mdns.start(
        runtime_id="witty-gizmo", port=8998,
        zc_factory=lambda: fake_zc,
        browser_factory=_fake_browser_factory(cb_holder),
        auto_connect=False,
    )
    _fire_added(fake_zc, "silly-droid._botgear._tcp.local.",
                peer_id="silly-droid", host_v4="10.0.0.5", port=8998)
    assert connect_calls == []


def test_discovery_ignores_records_without_id(monkeypatch):
    """A service on our type that lacks an 'id' property is some
    stranger's record — never auto-connect."""
    cb_holder: List = []
    fake_zc = _fake_zc_factory()
    connect_calls: List[str] = []
    monkeypatch.setattr(
        "botgear.runtime.peer_manager.connect",
        lambda url: connect_calls.append(url) or MagicMock(),
    )
    mdns.start(
        runtime_id="witty-gizmo", port=8998,
        zc_factory=lambda: fake_zc,
        browser_factory=_fake_browser_factory(cb_holder),
    )

    # Synthesize an Added event with no id prop
    import socket
    info = MagicMock()
    info.properties = {b"version": b"1.0.0"}     # missing 'id'
    info.addresses = [socket.inet_aton("10.0.0.5")]
    info.port = 8998
    fake_zc.get_service_info.return_value = info
    mdns._on_service_change(
        fake_zc, mdns._SERVICE_TYPE,
        "unknown._botgear._tcp.local.",
        _FakeStateChange.Added,
    )
    assert connect_calls == []


def test_discovery_ignores_removed_events(monkeypatch):
    """Removed events shouldn't trigger connect — we don't auto-
    disconnect on mDNS removal because a peer's mDNS record can
    flap (Wi-Fi flicker) while the WS connection stays healthy."""
    cb_holder: List = []
    fake_zc = _fake_zc_factory()
    connect_calls: List[str] = []
    monkeypatch.setattr(
        "botgear.runtime.peer_manager.connect",
        lambda url: connect_calls.append(url) or MagicMock(),
    )
    mdns.start(
        runtime_id="witty-gizmo", port=8998,
        zc_factory=lambda: fake_zc,
        browser_factory=_fake_browser_factory(cb_holder),
    )
    mdns._on_service_change(
        fake_zc, mdns._SERVICE_TYPE,
        "silly-droid._botgear._tcp.local.",
        _FakeStateChange.Removed,
    )
    assert connect_calls == []
