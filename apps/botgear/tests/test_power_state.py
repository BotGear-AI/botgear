# unmanaged
"""Unit tests for runtime/power_state.py — sysfs battery parsing, the
psutil cross-platform fallback, and the logind PrepareForSleep watcher.

The logind tests fake out jeepney's asyncio DBus client rather than
requiring a real system bus, so they run the same in CI/containers as on
a real Linux desktop.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from botgear.runtime import power_state
from botgear.runtime.bus import Bus


def _write_uevent(dir_path: Path, lines: list[str]) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "uevent").write_text("\n".join(lines) + "\n")


# ─── sysfs parser ──────────────────────────────────────────────────────


def test_sysfs_battery_and_ac(tmp_path):
    _write_uevent(tmp_path / "BAT0", [
        "POWER_SUPPLY_NAME=BAT0",
        "POWER_SUPPLY_TYPE=Battery",
        "POWER_SUPPLY_STATUS=Discharging",
        "POWER_SUPPLY_PRESENT=1",
        "POWER_SUPPLY_TECHNOLOGY=Li-ion",
        "POWER_SUPPLY_CYCLE_COUNT=42",
        "POWER_SUPPLY_VOLTAGE_NOW=11800000",
        "POWER_SUPPLY_CURRENT_NOW=1000000",
        "POWER_SUPPLY_CHARGE_FULL_DESIGN=5000000",
        "POWER_SUPPLY_CHARGE_FULL=4500000",
        "POWER_SUPPLY_CHARGE_NOW=2250000",
        "POWER_SUPPLY_CAPACITY=50",
        "POWER_SUPPLY_CAPACITY_LEVEL=Normal",
        "POWER_SUPPLY_MODEL_NAME=TestBatt",
        "POWER_SUPPLY_MANUFACTURER=TestCo",
    ])
    _write_uevent(tmp_path / "AC0", [
        "POWER_SUPPLY_NAME=AC0",
        "POWER_SUPPLY_TYPE=Mains",
        "POWER_SUPPLY_ONLINE=0",
    ])

    result = power_state._collect_linux_sysfs(tmp_path)

    assert result is not None
    battery = result["battery"]
    assert battery["present"] is True
    assert battery["name"] == "BAT0"
    assert battery["status"] == "discharging"
    assert battery["percent"] == 50.0
    assert battery["health_percent"] == 90.0
    assert battery["cycle_count"] == 42
    assert battery["voltage_v"] == pytest.approx(11.8)
    assert battery["current_a"] == pytest.approx(1.0)
    assert battery["energy_wh"] == pytest.approx(26.55)
    assert battery["energy_full_wh"] == pytest.approx(53.1)
    assert battery["energy_full_design_wh"] == pytest.approx(59.0)
    assert battery["secs_to_empty"] == 8100
    assert battery["secs_to_full"] is None
    assert battery["model"] == "TestBatt"
    assert battery["manufacturer"] == "TestCo"

    ac = result["ac"]
    assert ac == {"name": "AC0", "online": False}


def test_sysfs_charging_computes_secs_to_full(tmp_path):
    _write_uevent(tmp_path / "BAT0", [
        "POWER_SUPPLY_TYPE=Battery",
        "POWER_SUPPLY_STATUS=Charging",
        "POWER_SUPPLY_VOLTAGE_NOW=12000000",
        "POWER_SUPPLY_CURRENT_NOW=500000",
        "POWER_SUPPLY_CHARGE_FULL_DESIGN=5000000",
        "POWER_SUPPLY_CHARGE_FULL=5000000",
        "POWER_SUPPLY_CHARGE_NOW=4000000",
        "POWER_SUPPLY_CAPACITY=80",
    ])

    result = power_state._collect_linux_sysfs(tmp_path)
    battery = result["battery"]
    assert battery["status"] == "charging"
    assert battery["secs_to_full"] == 7200  # (5.0 - 4.0)Ah / 0.5A * 3600
    assert battery["secs_to_empty"] is None


def test_sysfs_missing_dir_returns_none(tmp_path):
    assert power_state._collect_linux_sysfs(tmp_path / "does-not-exist") is None


def test_sysfs_empty_dir_returns_none(tmp_path):
    assert power_state._collect_linux_sysfs(tmp_path) is None


# ─── psutil fallback ────────────────────────────────────────────────────


def test_psutil_fallback_used_when_no_sysfs(monkeypatch):
    monkeypatch.setattr(
        power_state.psutil, "sensors_battery",
        lambda: SimpleNamespace(percent=75.5, secsleft=1200, power_plugged=False),
    )
    result = power_state._collect_psutil_fallback()
    assert result["battery"]["percent"] == 75.5
    assert result["battery"]["status"] == "discharging"
    assert result["battery"]["secs_to_empty"] == 1200
    assert result["ac"] == {"name": None, "online": False}


def test_no_battery_anywhere(monkeypatch, tmp_path):
    monkeypatch.setattr(power_state, "SYSFS_POWER_SUPPLY_ROOT", tmp_path / "nope")
    monkeypatch.setattr(power_state.psutil, "sensors_battery", lambda: None)

    pub = power_state.PowerStatePublisher()
    snap = pub._snapshot()
    assert snap["battery"] is None
    assert snap["ac"] is None
    assert snap["platform_backend"] == "none"


def test_snapshot_uses_sysfs_when_present(monkeypatch, tmp_path):
    _write_uevent(tmp_path / "BAT0", [
        "POWER_SUPPLY_TYPE=Battery",
        "POWER_SUPPLY_STATUS=Full",
        "POWER_SUPPLY_CAPACITY=100",
    ])
    monkeypatch.setattr(power_state, "SYSFS_POWER_SUPPLY_ROOT", tmp_path)

    pub = power_state.PowerStatePublisher()
    snap = pub._snapshot()
    assert snap["platform_backend"] == "linux-sysfs"
    assert snap["battery"]["status"] == "full"
    assert snap["suspend"] == {
        "backend": "none", "supported": False,
        "last_event": None, "last_event_ts": None,
    }


# ─── logind PrepareForSleep watcher ─────────────────────────────────────


class _FailingRouterCtx:
    async def __aenter__(self):
        raise ConnectionRefusedError("no system bus in this environment")

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_logind_watcher_attach_failure_degrades_gracefully(monkeypatch, tmp_path):
    monkeypatch.setattr(power_state, "SYSFS_POWER_SUPPLY_ROOT", tmp_path / "nope")
    monkeypatch.setattr(power_state.psutil, "sensors_battery", lambda: None)
    monkeypatch.setattr(
        "jeepney.io.asyncio.open_dbus_router", lambda bus="SYSTEM": _FailingRouterCtx()
    )

    test_bus = Bus()
    monkeypatch.setattr(power_state, "get_bus", lambda: test_bus)

    pub = power_state.PowerStatePublisher(proxy_id="test-runtime", interval_s=60)
    pub.start()
    try:
        await asyncio.sleep(0.05)
        assert pub._suspend_supported is False
        assert pub._suspend_backend == "none"
        snap = pub._snapshot()
        assert snap["suspend"]["supported"] is False
        assert snap["suspend"]["backend"] == "none"
    finally:
        await pub.stop()


class _FakeFilterCtx:
    def __init__(self, queue: asyncio.Queue):
        self._queue = queue

    def __enter__(self):
        return self._queue

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeRouter:
    def __init__(self, queue: asyncio.Queue):
        self._queue = queue

    def filter(self, rule):
        return _FakeFilterCtx(self._queue)


class _FakeRouterCtx:
    def __init__(self, router: _FakeRouter):
        self._router = router

    async def __aenter__(self):
        return self._router

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeProxy:
    def __init__(self, *args, **kwargs):
        pass

    async def AddMatch(self, rule):
        return None


@pytest.mark.asyncio
async def test_logind_watcher_publishes_suspend_and_resume(monkeypatch, tmp_path):
    monkeypatch.setattr(power_state, "SYSFS_POWER_SUPPLY_ROOT", tmp_path / "nope")
    monkeypatch.setattr(power_state.psutil, "sensors_battery", lambda: None)

    signal_queue: asyncio.Queue = asyncio.Queue()
    await signal_queue.put(SimpleNamespace(body=(True,)))
    await signal_queue.put(SimpleNamespace(body=(False,)))
    fake_router = _FakeRouter(signal_queue)

    monkeypatch.setattr(
        "jeepney.io.asyncio.open_dbus_router",
        lambda bus="SYSTEM": _FakeRouterCtx(fake_router),
    )
    monkeypatch.setattr("jeepney.io.asyncio.Proxy", _FakeProxy)

    test_bus = Bus()
    monkeypatch.setattr(power_state, "get_bus", lambda: test_bus)

    pub = power_state.PowerStatePublisher(proxy_id="test-runtime", interval_s=60)
    pub.start()
    try:
        events = []
        async for msg in test_bus.subscribe(pub.event_topic, "test-subscriber"):
            events.append(msg.payload)
            if len(events) == 2:
                break
        assert events[0]["type"] == "suspend"
        assert events[1]["type"] == "resume"
        assert "ts" in events[0] and "ts" in events[1]

        assert pub._last_event == "resume"
        assert test_bus.has_retained(pub.topic)
    finally:
        await pub.stop()
