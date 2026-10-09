# unmanaged
"""Unit tests for the tasmota subprocess service.

Covers the pure protocol helpers in tasmota_client.py (URL building,
relay auto-detection, PulseTime encoding, scan classification) and the
TasmotaService @service_method actions (mocking TasmotaClient + the
bus). No real network I/O — httpx is already a dependency of the
top-level botgear venv (unlike arduino's pymata4/pyserial), so the
service package imports cleanly here with no module stubbing needed.

The tasmota service lives in its own subprocess venv with its own
pyproject. We add its src/ to sys.path so pytest can import it from the
top-level botgear venv, mirroring test_arduino_service.py.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

_TASMOTA_SRC = Path(__file__).resolve().parents[1] / "repo" / "tasmota" / "1.0.0" / "src"
if str(_TASMOTA_SRC) not in sys.path:
    sys.path.insert(0, str(_TASMOTA_SRC))


# ─────────────────────────────────────────────────────────────────────
# tasmota_client — pure protocol helpers
# ─────────────────────────────────────────────────────────────────────


def test_build_command_url_encodes_space_and_omits_absent_auth():
    from tasmota_service.tasmota_client import build_command_url
    url = build_command_url("192.168.0.50", "Power1 ON")
    assert url == "http://192.168.0.50/cm?cmnd=Power1%20ON"


def test_build_command_url_includes_auth_when_given():
    from tasmota_service.tasmota_client import build_command_url
    url = build_command_url("192.168.0.50", "Status 0", username="admin", password="joker")
    assert url == "http://192.168.0.50/cm?cmnd=Status%200&user=admin&password=joker"


def test_build_command_url_omits_password_when_blank():
    from tasmota_service.tasmota_client import build_command_url
    url = build_command_url("h", "Power1 ON", username="admin", password="")
    assert "password=" not in url
    assert "user=admin" in url


@pytest.mark.parametrize("ms,expected", [
    (0, 1),
    (100, 1),        # 0.1s -> value 1
    (5000, 50),       # 5.0s -> value 50 (0.1s increments)
    (11100, 111),     # boundary: 11.1s -> value 111 (top of the 0.1s range)
    (11200, 112),     # just over the boundary -> 1s-increment range, 100+12
    (30000, 130),     # 30s -> 100+30
    (999999999, 64900),  # clamps to the encoding's ceiling
])
def test_ms_to_pulsetime_encoding(ms, expected):
    from tasmota_service.tasmota_client import ms_to_pulsetime
    assert ms_to_pulsetime(ms) == expected


def test_parse_relays_single_relay_device():
    from tasmota_service.tasmota_client import parse_relays
    relays = parse_relays({"POWER": "ON"}, ["Sonoff Basic"])
    assert relays == [{"index": 1, "on": True, "label": "Sonoff Basic"}]


def test_parse_relays_multi_relay_device_with_per_relay_names():
    from tasmota_service.tasmota_client import parse_relays
    sts = {"POWER1": "ON", "POWER2": "OFF", "POWER3": "ON", "POWER4": "OFF"}
    names = ["Living Room", "Kitchen", "Porch", "Garage"]
    relays = parse_relays(sts, names)
    assert relays == [
        {"index": 1, "on": True, "label": "Living Room"},
        {"index": 2, "on": False, "label": "Kitchen"},
        {"index": 3, "on": True, "label": "Porch"},
        {"index": 4, "on": False, "label": "Garage"},
    ]


def test_parse_relays_falls_back_to_generic_label_when_no_friendly_name():
    from tasmota_service.tasmota_client import parse_relays
    relays = parse_relays({"POWER1": "ON"})
    assert relays == [{"index": 1, "on": True, "label": "Relay 1"}]


def test_parse_relays_empty_when_no_power_keys():
    from tasmota_service.tasmota_client import parse_relays
    assert parse_relays({"Some": "OtherKey"}) == []


@pytest.mark.parametrize("payload,expected", [
    ({"Status": {"Module": 1}}, True),
    ({"StatusSTS": {"POWER": "ON"}}, False),   # no top-level Status key
    ({"Status": "not-a-dict"}, False),
    (None, False),
    ("not even a dict", False),
    ([], False),
])
def test_is_tasmota_status_classifier(payload, expected):
    from tasmota_service.tasmota_client import is_tasmota_status
    assert is_tasmota_status(payload) is expected


# ─────────────────────────────────────────────────────────────────────
# TasmotaClient — command construction against a mocked httpx
# ─────────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, json_body: Any, status_code: int = 200) -> None:
        self._json = json_body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._json


class _FakeAsyncClient:
    """Records every GET url; returns queued responses in order (or the
    single ``default_response`` for every call if no queue is set)."""

    def __init__(self, default_response: Any = None) -> None:
        self.urls: List[str] = []
        self.default_response = default_response if default_response is not None else _FakeResponse({})
        self.responses: List[Any] = []

    def __call__(self, *args, **kwargs) -> "_FakeAsyncClient":
        return self

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def get(self, url: str) -> _FakeResponse:
        self.urls.append(url)
        if self.responses:
            return self.responses.pop(0)
        return self.default_response


@pytest.mark.asyncio
async def test_client_set_power_uses_numbered_relay_command(monkeypatch):
    import tasmota_service.tasmota_client as tc
    fake = _FakeAsyncClient(_FakeResponse({"POWER2": "ON"}))
    monkeypatch.setattr(tc, "httpx", MagicMock(AsyncClient=fake))
    client = tc.TasmotaClient("10.0.0.5")
    await client.set_power(2, True)
    assert fake.urls == ["http://10.0.0.5/cm?cmnd=Power2%20ON"]


@pytest.mark.asyncio
async def test_client_pulse_sets_pulsetime_then_powers_on(monkeypatch):
    import tasmota_service.tasmota_client as tc
    fake = _FakeAsyncClient(_FakeResponse({}))
    monkeypatch.setattr(tc, "httpx", MagicMock(AsyncClient=fake))
    client = tc.TasmotaClient("10.0.0.5")
    await client.pulse(1, 5000)
    assert fake.urls == [
        "http://10.0.0.5/cm?cmnd=PulseTime1%2050",
        "http://10.0.0.5/cm?cmnd=Power1%20ON",
    ]


# ─────────────────────────────────────────────────────────────────────
# scan_subnet — classifier + concurrency glue
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scan_subnet_keeps_only_valid_tasmota_responses():
    from tasmota_service.tasmota_client import scan_subnet

    class _ScanClient(_FakeAsyncClient):
        async def get(self, url: str) -> _FakeResponse:
            self.urls.append(url)
            if "10.0.0.5" in url:
                return _FakeResponse({"Status": {"FriendlyName": ["Porch Light"], "Module": 18}})
            if "10.0.0.6" in url:
                raise TimeoutError("simulated timeout")
            if "10.0.0.7" in url:
                return _FakeResponse({"not_status": True})   # valid JSON, not Tasmota
            if "10.0.0.8" in url:
                return _FakeResponse({}, status_code=404)
            return _FakeResponse("not json at all")           # .json() would raise for a real client

    client = _ScanClient()
    candidates = await scan_subnet(
        "10.0.0", client_factory=lambda: client, host_suffixes=range(5, 9),
    )
    assert candidates == [{"host": "10.0.0.5", "friendly_name": "Porch Light", "module": 18}]


# ─────────────────────────────────────────────────────────────────────
# TasmotaService @service_method actions
# ─────────────────────────────────────────────────────────────────────


class _FakeBus:
    """Minimal BusClient stand-in — mirrors test_arduino_service.py's."""

    def __init__(self) -> None:
        self.published: List[Dict[str, Any]] = []

    async def publish(self, topic: str, payload: Any, *, retained: bool = False) -> None:
        self.published.append({"topic": topic, "payload": payload, "retained": retained})

    async def subscribe(self, topic: str, handler):
        pass

    def last(self, suffix: str) -> Dict[str, Any]:
        matches = [m for m in self.published if m["topic"].endswith(suffix)]
        assert matches, f"no publish to a topic ending in {suffix!r}"
        return matches[-1]


@pytest.fixture
def service(monkeypatch):
    from tasmota_service.service import TasmotaService

    svc = TasmotaService("tasmota-test", _FakeBus())
    svc._update_config_calls: List[Dict[str, Any]] = []  # type: ignore[attr-defined]

    async def _record_update(patch):
        svc._update_config_calls.append(patch)  # type: ignore[attr-defined]
        for k, v in patch.items():
            setattr(svc.config, k, v)
    monkeypatch.setattr(svc, "update_config", _record_update)
    return svc


@pytest.mark.asyncio
async def test_power_set_with_no_host_returns_error(service):
    result = await service.m_power_set(relay=1, on=True)
    assert result == {"error": "no host configured — call connect first"}


@pytest.mark.asyncio
async def test_power_set_issues_command_and_updates_state_optimistically(service, monkeypatch):
    service.config.host = "10.0.0.5"
    fake_client = MagicMock()
    fake_client.set_power = AsyncMock()
    monkeypatch.setattr(service, "_client", lambda: fake_client)

    result = await service.m_power_set(relay=1, on=True)

    fake_client.set_power.assert_awaited_once_with(1, True)
    assert result == {"relay": 1, "on": True}
    # /state published immediately — doesn't wait for the next poll tick.
    published = service.bus.last("state")
    assert published["retained"] is True
    assert published["payload"]["relays"] == [{"index": 1, "on": True, "label": "Relay 1"}]


@pytest.mark.asyncio
async def test_power_toggle_flips_cached_state(service, monkeypatch):
    service.config.host = "10.0.0.5"
    service._relays = [{"index": 1, "on": False, "label": "Relay 1"}]
    fake_client = MagicMock()
    fake_client.toggle_power = AsyncMock()
    monkeypatch.setattr(service, "_client", lambda: fake_client)

    result = await service.m_power_toggle(relay=1)

    assert result == {"relay": 1, "on": True}
    assert service._relays[0]["on"] is True


@pytest.mark.asyncio
async def test_pulse_marks_relay_on(service, monkeypatch):
    service.config.host = "10.0.0.5"
    fake_client = MagicMock()
    fake_client.pulse = AsyncMock()
    monkeypatch.setattr(service, "_client", lambda: fake_client)

    result = await service.m_pulse(relay=2, ms=3000)

    fake_client.pulse.assert_awaited_once_with(2, 3000)
    assert result == {"relay": 2, "ms": 3000}
    assert service._find_relay(2) == {"index": 2, "on": True, "label": "Relay 2"}


@pytest.mark.asyncio
async def test_connect_success_applies_status_and_returns_snapshot(service, monkeypatch):
    fake_client = MagicMock()
    fake_client.status = AsyncMock(return_value={
        "Status": {"FriendlyName": ["Shop Switch"], "Module": 1},
        "StatusSTS": {"POWER": "ON"},
    })
    monkeypatch.setattr(service, "_client", lambda: fake_client)

    result = await service.m_connect(host="10.0.0.5")

    assert service._update_config_calls[0]["host"] == "10.0.0.5"
    assert result["reachable"] is True
    assert result["friendly_name"] == "Shop Switch"
    assert result["relays"] == [{"index": 1, "on": True, "label": "Shop Switch"}]


@pytest.mark.asyncio
async def test_connect_failure_reports_error_without_raising(service, monkeypatch):
    fake_client = MagicMock()
    fake_client.status = AsyncMock(side_effect=TimeoutError("no route"))
    monkeypatch.setattr(service, "_client", lambda: fake_client)

    result = await service.m_connect(host="10.0.0.99")

    assert result == {"error": "could not reach 10.0.0.99"}


@pytest.mark.asyncio
async def test_disconnect_clears_relays_and_host(service):
    service.config.host = "10.0.0.5"
    service._relays = [{"index": 1, "on": True, "label": "x"}]
    service._reachable = True

    result = await service.m_disconnect()

    assert result == {"disconnected": True}
    assert service.config.host is None
    assert service._relays == []
    assert service._reachable is False


@pytest.mark.asyncio
async def test_list_relays_returns_cache_with_no_network_call(service):
    service._relays = [{"index": 1, "on": True, "label": "Shop"}]
    result = await service.m_list_relays()
    assert result == {"relays": [{"index": 1, "on": True, "label": "Shop"}]}


@pytest.mark.asyncio
async def test_poll_once_marks_unreachable_after_threshold(service, monkeypatch):
    from tasmota_service.service import _UNREACHABLE_AFTER
    service.config.host = "10.0.0.5"
    service._reachable = True
    fake_client = MagicMock()
    fake_client.status = AsyncMock(side_effect=TimeoutError("gone"))
    monkeypatch.setattr(service, "_client", lambda: fake_client)

    for _ in range(_UNREACHABLE_AFTER - 1):
        await service._poll_once()
        assert service._reachable is True   # debounced — not flipped yet

    await service._poll_once()
    assert service._reachable is False


@pytest.mark.asyncio
async def test_poll_once_publishes_sensor_topic_only_when_present(service, monkeypatch):
    fake_client = MagicMock()
    fake_client.status = AsyncMock(return_value={
        "Status": {"FriendlyName": ["Pow"], "Module": 1},
        "StatusSTS": {"POWER": "ON"},
        "StatusSNS": {"ENERGY": {"Power": 12.3}},
    })
    monkeypatch.setattr(service, "_client", lambda: fake_client)

    await service._poll_once()

    sensor_pub = service.bus.last("sensor")
    assert sensor_pub["payload"] == {"ENERGY": {"Power": 12.3}}
    assert sensor_pub["retained"] is True
