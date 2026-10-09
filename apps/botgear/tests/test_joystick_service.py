# unmanaged
"""Unit tests for the joystick subprocess service.

Two layers:
  * pure poller helpers (quantize_axis, inputs_changed,
    should_publish_input) — the deadzone + change-detection +
    heartbeat logic that gates what reaches the bus. No pygame.
  * JoystickService @service_method actions + poller callbacks, driven
    with a fake poller + no-op bus, so we assert on the state/input the
    service publishes without opening a real device.

pygame is imported lazily inside Poller._run (the thread body), so
importing joystick_service.poller / .service here in the top-level test
venv — which has no pygame — is fine.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_JS_SRC = Path(__file__).resolve().parents[1] / "repo" / "joystick" / "1.0.0" / "src"
if str(_JS_SRC) not in sys.path:
    sys.path.insert(0, str(_JS_SRC))


from joystick_service import poller as poller_mod  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# pure helpers
# ─────────────────────────────────────────────────────────────────────


def test_quantize_axis_applies_deadzone():
    assert poller_mod.quantize_axis(0.02, 0.05) == 0.0     # inside deadzone
    assert poller_mod.quantize_axis(-0.04, 0.05) == 0.0
    assert poller_mod.quantize_axis(0.5, 0.05) == 0.5      # outside → kept
    assert poller_mod.quantize_axis(-1.0, 0.05) == -1.0


def test_quantize_axis_rounds():
    # noisy low bits collapse to a stable rounded value
    assert poller_mod.quantize_axis(0.123456, 0.05, ndigits=3) == 0.123


def test_inputs_changed():
    a = {"axes": [0.0], "buttons": [0], "hats": [[0, 0]], "balls": []}
    assert poller_mod.inputs_changed(None, a) is True       # first read
    assert poller_mod.inputs_changed(a, dict(a)) is False    # identical
    b = {**a, "buttons": [1]}
    assert poller_mod.inputs_changed(a, b) is True           # a button changed
    c = {**a, "axes": [0.5]}
    assert poller_mod.inputs_changed(a, c) is True           # an axis moved


def test_should_publish_input_on_actual_change_regardless_of_heartbeat():
    a = {"axes": [0.0], "buttons": [0], "hats": [], "balls": []}
    b = {**a, "axes": [0.5]}
    # last_publish == now: heartbeat interval hasn't elapsed at all, but
    # a real change still publishes immediately.
    assert poller_mod.should_publish_input(
        a, b, now=10.0, last_publish=10.0, heartbeat_interval_s=0.2,
    ) is True


def test_should_publish_input_suppressed_when_unchanged_and_within_heartbeat():
    a = {"axes": [0.5], "buttons": [0], "hats": [], "balls": []}
    assert poller_mod.should_publish_input(
        a, dict(a), now=10.1, last_publish=10.0, heartbeat_interval_s=0.2,
    ) is False


def test_should_publish_input_heartbeats_a_held_steady_stick():
    """The bug this exists to fix: a stick held at a CONSTANT non-zero
    deflection (the ordinary case while driving, not an edge case) must
    still republish at the heartbeat rate — otherwise a downstream
    liveness watchdog (motor_control's input_source timeout) sees
    "unchanged" as "gone silent" and zeros the channel mid-drive."""
    held = {"axes": [-0.99], "buttons": [0], "hats": [], "balls": []}
    assert poller_mod.should_publish_input(
        held, dict(held), now=10.3, last_publish=10.0, heartbeat_interval_s=0.2,
    ) is True


def test_should_publish_input_first_read_always_publishes():
    a = {"axes": [0.0], "buttons": [0], "hats": [], "balls": []}
    assert poller_mod.should_publish_input(
        None, a, now=0.0, last_publish=0.0, heartbeat_interval_s=0.2,
    ) is True


# ─────────────────────────────────────────────────────────────────────
# service + fake poller
# ─────────────────────────────────────────────────────────────────────


class FakePoller:
    """Records the commands the service sends; never touches pygame."""

    def __init__(self) -> None:
        self.calls: List[Any] = []
        self.started = False

    def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    def attach(self, index: int) -> None:
        self.calls.append(("attach", index))

    def detach(self) -> None:
        self.calls.append(("detach", None))

    def rescan(self) -> None:
        self.calls.append(("rescan", None))

    def set_enabled(self, enabled: bool) -> None:
        self.calls.append(("enabled", enabled))

    def set_params(self, poll_hz, deadzone) -> None:
        self.calls.append(("params", (poll_hz, deadzone)))


_DEVICE = {
    "index": 0, "name": "Test Pad", "guid": "abc123",
    "num_axes": 4, "num_buttons": 10, "num_hats": 1, "num_balls": 0,
}


@pytest.fixture
def js(monkeypatch):
    from unittest.mock import MagicMock
    from joystick_service.service import JoystickService

    svc = JoystickService("joystick-1", MagicMock())

    published: List[Dict[str, Any]] = []

    async def _publish(suffix: str, payload: Any, *, retained: bool = False) -> None:
        published.append({"suffix": suffix, "payload": payload, "retained": retained})

    async def _merge_update(updates):
        svc.config = svc.config.merge_dict(updates)

    monkeypatch.setattr(svc, "publish", _publish)
    monkeypatch.setattr(svc, "update_config", _merge_update)
    fake = FakePoller()
    svc._poller = fake
    svc._published = published   # type: ignore[attr-defined]
    svc._fake = fake             # type: ignore[attr-defined]
    return svc


def _states(svc) -> List[Dict[str, Any]]:
    return [p["payload"] for p in svc._published if p["suffix"] == "state"]


@pytest.mark.asyncio
async def test_attach_forwards_index_to_poller(js):
    await js.m_attach(index=2)
    assert ("attach", 2) in js._fake.calls


@pytest.mark.asyncio
async def test_on_attached_publishes_components_and_persists(js):
    await js._on_attached(_DEVICE)
    st = _states(js)[-1]
    assert st["attached"] is True
    # component counts come straight from the device meta
    assert st["components"] == {"axes": 4, "buttons": 10, "hats": 1, "balls": 0}
    # the device + guid are persisted for next-session pre-select
    assert js.config.last_index == 0
    assert js.config.last_guid == "abc123"


@pytest.mark.asyncio
async def test_on_devices_lists_in_state(js):
    await js._on_devices([_DEVICE])
    st = _states(js)[-1]
    assert st["devices"] == [_DEVICE]
    assert st["attached"] is False   # listing isn't attaching


@pytest.mark.asyncio
async def test_on_input_streams_to_input_topic(js):
    snap = {"axes": [0.5, 0.0], "buttons": [1], "hats": [[0, 1]], "balls": [], "ts": 1.0}
    await js._on_input(snap)
    inputs = [p["payload"] for p in js._published if p["suffix"] == "input"]
    assert inputs == [snap]
    # input is a stream — never retained
    assert all(not p["retained"] for p in js._published if p["suffix"] == "input")


@pytest.mark.asyncio
async def test_set_enabled_persists_and_forwards(js):
    await js.m_set_enabled(enabled=False)
    assert js.config.enabled is False
    assert ("enabled", False) in js._fake.calls


@pytest.mark.asyncio
async def test_set_params_clamps_and_forwards(js):
    await js.m_set_params(poll_hz=9999, deadzone=2.0)
    assert js.config.poll_hz == 250        # clamped to max
    assert js.config.deadzone == 1.0       # clamped to max
    assert ("params", (250, 1.0)) in js._fake.calls


@pytest.mark.asyncio
async def test_detach_clears_attachment(js):
    await js._on_attached(_DEVICE)
    await js.m_detach()
    assert ("detach", None) in js._fake.calls
    # simulate the poller's detached callback
    await js._on_attached(None)
    assert _states(js)[-1]["attached"] is False


@pytest.mark.asyncio
async def test_error_surfaces_in_state(js):
    await js._on_error("attached device disconnected")
    assert _states(js)[-1]["last_error"] == "attached device disconnected"


# ─────────────────────────────────────────────────────────────────────
# auto-connect — background retry loop
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_auto_connect_enable_starts_task_and_persists_config(js):
    assert js.config.autoreconnect is False
    await js.m_set_auto_connect(enabled=True)
    assert js.config.autoreconnect is True
    assert js._auto_connect_task is not None
    assert not js._auto_connect_task.done()
    js._stop_auto_connect_loop()  # don't leak a running task past the test


@pytest.mark.asyncio
async def test_set_auto_connect_disable_stops_task(js):
    await js.m_set_auto_connect(enabled=True)
    await js.m_set_auto_connect(enabled=False)
    assert js.config.autoreconnect is False
    assert js._auto_connect_task is None


@pytest.mark.asyncio
async def test_detach_stops_auto_connect_task(js):
    await js.m_set_auto_connect(enabled=True)
    await js.m_detach()
    assert js.config.autoreconnect is False
    assert js._auto_connect_task is None


@pytest.mark.asyncio
async def test_attach_arms_auto_connect_loop(js):
    await js._on_attached(_DEVICE)
    assert js._auto_connect_task is not None
    js._stop_auto_connect_loop()


def test_find_reconnect_target_prefers_guid_over_index(js):
    # Stale index (from before a replug) shouldn't matter once a GUID
    # match is available at a different index.
    js.config = js.config.merge_dict({"last_guid": "abc123", "last_index": 7})
    js._devices = [{**_DEVICE, "index": 3, "guid": "abc123"}]
    assert js._find_reconnect_target() == 3


def test_find_reconnect_target_waits_when_guid_absent(js):
    js.config = js.config.merge_dict({"last_guid": "abc123", "last_index": 0})
    js._devices = []  # target not currently plugged in
    assert js._find_reconnect_target() is None


def test_find_reconnect_target_falls_back_to_index_without_guid(js):
    js.config = js.config.merge_dict({"last_guid": None, "last_index": 2})
    assert js._find_reconnect_target() == 2


@pytest.mark.asyncio
async def test_snapshot_reports_retrying_when_detached_with_autoreconnect(js):
    js._device = None
    js.config = js.config.merge_dict({"autoreconnect": True})
    assert js._snapshot()["retrying"] is True
    js.config = js.config.merge_dict({"autoreconnect": False})
    assert js._snapshot()["retrying"] is False
