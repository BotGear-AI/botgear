# unmanaged
"""Runtime-wide HALT: the runtime's status tracker + REST surface, the
framework hooks every service gets, and the brain's reaction (cancel all
runs, refuse new ones, no keepalive resurrection). Audio/keyboard halt
behaviour is covered in test_rlx_audio.py / test_rlx_input.py.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

_BRAIN_PKG = Path(__file__).resolve().parents[1] / "repo" / "brain" / "1.0.0"
sys.path.insert(0, str(_BRAIN_PKG))

from rlx_bus import HALT_STATE_TOPIC, HALT_TOPIC, RESUME_TOPIC, SubprocessService, halt_event  # noqa: E402

from botgear.runtime import halt  # noqa: E402


class _Bus:
    def __init__(self) -> None:
        self.published: List[tuple] = []

    def publish_sync(self, topic: str, payload: Any, *, retained: bool = False, **_: Any) -> int:
        self.published.append((topic, payload, retained))
        return 1

    def on(self, topic: str) -> List[Any]:
        return [p for t, p, _ in self.published if t == topic]


@pytest.fixture
def bus(monkeypatch):
    fake = _Bus()
    monkeypatch.setattr(halt, "get_bus", lambda: fake)
    monkeypatch.setattr(halt, "_state", {"halted": False, "level": None, "source": None,
                                         "reason": None, "since": None, "count": 0})
    halt._seen.clear()
    return fake


# ─── rlx_bus.halt payload ─────────────────────────────────────────────


def test_halt_event_coerces_unknown_level_to_stop():
    assert halt_event("pause-ish", "x")["level"] == "stop"
    assert halt_event("emergency", "x")["level"] == "emergency"
    assert halt_event()["id"] != halt_event()["id"]


# ─── runtime/halt.py ──────────────────────────────────────────────────


def test_request_halt_broadcasts_and_latches(bus):
    st = halt.request_halt("stop", source="ui:op", reason="Stop button")
    assert st["halted"] is True and st["level"] == "stop" and st["source"] == "ui:op"
    assert halt.is_halted() is True
    assert len(bus.on(HALT_TOPIC)) == 1
    assert bus.on(HALT_STATE_TOPIC)[-1]["halted"] is True
    assert [r for t, _, r in bus.published if t == HALT_STATE_TOPIC] == [True]   # retained


def test_request_resume_clears(bus):
    halt.request_halt()
    st = halt.request_resume(source="ui:op")
    assert st["halted"] is False and halt.is_halted() is False
    assert len(bus.on(RESUME_TOPIC)) == 1


def test_bus_echo_of_own_request_is_not_double_counted(bus):
    halt.request_halt()
    ev = bus.on(HALT_TOPIC)[0]
    halt._apply("halt", ev)                       # the listener sees our own publish
    assert halt.state()["count"] == 1


def test_halt_from_another_service_is_tracked(bus):
    halt._apply("halt", halt_event("stop", "voice:ear", "heard 'stop'"))
    assert halt.state()["source"] == "voice:ear" and halt.is_halted()


def test_plain_stop_never_downgrades_an_emergency(bus):
    halt._apply("halt", halt_event("emergency", "voice:ear"))
    halt._apply("halt", halt_event("stop", "keyboard:kb"))
    assert halt.state()["level"] == "emergency"


# ─── REST ─────────────────────────────────────────────────────────────


def test_system_api_halt_and_resume(bus):
    from botgear.api import system_api
    st = system_api.post_system_halt(system_api.HaltRequest(level="emergency"), {"email": "op@x"})
    assert st["halted"] is True and st["level"] == "emergency" and st["source"] == "ui:op@x"
    assert system_api.get_system_halt(None)["halted"] is True
    st = system_api.post_system_resume(system_api.ResumeRequest(), {"email": "op@x"})
    assert st["halted"] is False


# ─── framework hooks ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subprocess_service_halt_hook_is_isolated():
    class _Svc(SubprocessService):
        type_name = "thing"

        def __init__(self) -> None:
            super().__init__("thing-1", SimpleNamespace())
            self.halts: List[Dict[str, Any]] = []

        async def on_halt(self, event):
            self.halts.append(event)
            raise RuntimeError("buggy handler")

    svc = _Svc()
    await svc._on_halt_msg({"level": "stop"})      # raising hook is swallowed + logged
    await svc._on_halt_msg("garbage")              # malformed payload → empty event
    assert svc.halts == [{"level": "stop"}, {}]


def test_in_process_service_has_noop_hooks():
    from botgear.framework.service import Service
    assert asyncio.iscoroutinefunction(Service.on_halt)
    assert asyncio.iscoroutinefunction(Service.on_resume)


# ─── brain ───────────────────────────────────────────────────────────


class _FakeEngine:
    def __init__(self, exit_via_phrase: bool = False) -> None:
        self.cancelled_with = None
        self._exit_via_phrase = exit_via_phrase

    def cancel(self, reason: str) -> None:
        self.cancelled_with = reason

    async def run(self) -> None:
        return None


def _fake_brain(**runs) -> SimpleNamespace:
    from brain.service import BrainService
    fake = SimpleNamespace(
        proxy_id="brain",
        _runs=dict(runs),
        _run_tasks={},
        _keepalive={rid: {"name": "general_assistant"} for rid in runs},
        restarts=[],
    )
    fake.m_cancel = lambda run_id, reason="": BrainService.m_cancel(fake, run_id=run_id, reason=reason)

    async def _publish_state():
        return None

    async def _keepalive_restart(args):
        fake.restarts.append(args)

    fake._publish_state = _publish_state
    fake._keepalive_restart = _keepalive_restart
    return fake


@pytest.mark.asyncio
async def test_brain_on_halt_cancels_every_run_and_drops_keepalive():
    from brain.service import BrainService
    a, b = _FakeEngine(), _FakeEngine()
    fake = _fake_brain(r1=a, r2=b)
    await BrainService.on_halt(fake, {"level": "stop", "source": "voice:ear", "reason": "heard 'stop'"})
    assert a.cancelled_with == b.cancelled_with == "halted by voice:ear: heard 'stop'"
    assert fake._keepalive == {}


@pytest.mark.asyncio
async def test_brain_refuses_to_start_while_halted(monkeypatch):
    from brain.service import BrainService
    monkeypatch.setattr(halt, "is_halted", lambda: True)
    with pytest.raises(RuntimeError, match="HALTED"):
        await BrainService.m_start_workflow(_fake_brain(), name="general_assistant")


@pytest.mark.asyncio
async def test_keepalive_not_restarted_after_exit_phrase(monkeypatch):
    from brain.service import BrainService
    monkeypatch.setattr(halt, "is_halted", lambda: False)
    engine = _FakeEngine(exit_via_phrase=True)
    fake = _fake_brain(r1=engine)
    await BrainService._run_to_completion(fake, "r1", engine)
    await asyncio.sleep(0)
    assert fake.restarts == []


@pytest.mark.asyncio
async def test_keepalive_not_restarted_while_halted(monkeypatch):
    from brain.service import BrainService
    monkeypatch.setattr(halt, "is_halted", lambda: True)
    engine = _FakeEngine()
    fake = _fake_brain(r1=engine)
    await BrainService._run_to_completion(fake, "r1", engine)
    await asyncio.sleep(0)
    assert fake.restarts == []


@pytest.mark.asyncio
async def test_keepalive_still_restarts_after_an_ordinary_end(monkeypatch):
    from brain.service import BrainService
    monkeypatch.setattr(halt, "is_halted", lambda: False)
    engine = _FakeEngine()
    fake = _fake_brain(r1=engine)
    await BrainService._run_to_completion(fake, "r1", engine)
    await asyncio.sleep(0)
    assert fake.restarts == [{"name": "general_assistant"}]
