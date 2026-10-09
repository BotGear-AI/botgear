import asyncio
import sys
from pathlib import Path
from typing import Dict, Any

import pytest

# Put the brain and fast_decision service packages on the import path.
_BRAIN_PKG = Path(__file__).resolve().parents[1] / "repo" / "brain" / "1.0.0"
_DECISION_PKG = Path(__file__).resolve().parents[1] / "repo" / "fast_decision" / "1.0.0"
if str(_BRAIN_PKG) not in sys.path:
    sys.path.insert(0, str(_BRAIN_PKG))
if str(_DECISION_PKG) not in sys.path:
    sys.path.insert(0, str(_DECISION_PKG))

from botgear.agent_surface.models import ToolDescriptor
from botgear.framework.service import ServiceMetadata
from brain.schemas import Workflow, ProposedAction, ModelResponse
from brain.reflex_engine import FastReflexEngine
from rlx_decision import (
    Noul,
    Choice,
    Score,
    DecisionClient,
    MockDecisionClient,
    create_decision_client,
)
from service import FastDecisionService, FastDecisionConfig


@pytest.mark.asyncio
async def test_rlx_decision_primitives():
    client = create_decision_client(mock=True)

    # Noul
    p = await client.decide_noul("turn off the light", "Is this a lighting command?")
    assert p >= 0.90

    p_false = await client.decide_noul("what is the weather in seattle", "Is this a lighting command?")
    assert p_false <= 0.20

    # Choice
    c = await client.decide_choice(
        "turn off the light",
        "What is the requested power state?",
        ["on", "off", "toggle", "none"],
    )
    assert c.choice == "off"
    assert c.confidence >= 0.95

    # Score
    s = await client.decide_score(
        "critical obstacle detected at 10cm",
        "Rate collision danger.",
        ["safe", "caution", "danger", "critical"],
    )
    assert s.score >= 3.0
    assert s.confidence >= 0.90


@pytest.mark.asyncio
async def test_fast_decision_service():
    meta = ServiceMetadata(
        proxy_id="decision-1",
        service_meta_id="fast_decision@1.0.0",
        type_name="fast_decision",
        type_version="1.0.0",
        tags=[],
        singleton=False,
    )
    service = FastDecisionService(
        meta=meta,
        config={"residency": "local_only", "confidence_threshold": 0.95},
    )
    service.client = create_decision_client(mock=True)

    # Test lighting reflex evaluation
    res = await service.evaluate_lighting(
        "turn off the office light",
        registered_lights=["office_light", "workbench"],
    )
    assert res["is_direct"] is True
    assert res["target_device"] == "office_light"
    assert res["target_state"] == "off"
    assert "turning off" in res["spoken_ack"]
    assert res["confidence"] >= 0.95

    # Test pan-tilt gaze reflex evaluation
    gaze_res = await service.evaluate_pan_tilt("look up and tell me what you see")
    assert gaze_res["is_gaze_command"] is True
    assert gaze_res["target_pose"] == "look_up"
    assert gaze_res["requires_llm_followup"] is True

    # Test HALT
    await service.on_halt({"reason": "emergency_test"})
    halted_res = await service.evaluate_lighting("turn off the office light")
    assert halted_res.get("halted") is True

    await service.on_stop()


@pytest.mark.asyncio
async def test_fast_reflex_engine_lighting():
    engine = FastReflexEngine()

    catalog = {
        "/tasmota/office-light/control::power_set": ToolDescriptor(
            topic="/tasmota/office-light/control", action="power_set"
        ),
        "/tasmota/workbench/control::power_set": ToolDescriptor(
            topic="/tasmota/workbench/control", action="power_set"
        ),
        "/speech/local/control::speak": ToolDescriptor(
            topic="/speech/local/control", action="speak"
        ),
    }

    # Direct command: "turn the office light off"
    reflex = await engine.evaluate_utterance("turn the office light off", catalog)
    assert reflex is not None
    assert reflex.reflex_kind == "lighting_reflex"
    assert len(reflex.actions) == 2

    # First action: power_set(on=False) to /tasmota/office-light/control
    actuation = reflex.actions[0]
    assert actuation.topic == "/tasmota/office-light/control"
    assert actuation.action == "power_set"
    assert actuation.args == {"on": False}

    # Second action: speak("turning off the light")
    speech = reflex.actions[1]
    assert speech.topic == "/speech/local/control"
    assert speech.action == "speak"
    assert "turning off" in speech.args["text"]

    # Complex / general query should return None (fall through to LLM)
    general_reflex = await engine.evaluate_utterance(
        "why is the sky blue and who was Napoleon", catalog
    )
    assert general_reflex is None


@pytest.mark.asyncio
async def test_fast_reflex_engine_pan_tilt():
    engine = FastReflexEngine()

    catalog = {
        "/servo/tilt/control::servo_write": ToolDescriptor(
            topic="/servo/tilt/control", action="servo_write"
        ),
        "/servo/pan/control::servo_write": ToolDescriptor(
            topic="/servo/pan/control", action="servo_write"
        ),
        "/speech/local/control::speak": ToolDescriptor(
            topic="/speech/local/control", action="speak"
        ),
    }

    # "look up"
    reflex = await engine.evaluate_utterance("look up", catalog)
    assert reflex is not None
    assert reflex.reflex_kind == "pan_tilt_reflex"
    assert len(reflex.actions) == 2

    servo_action = reflex.actions[0]
    assert servo_action.topic == "/servo/tilt/control"
    assert servo_action.action == "servo_write"
    assert servo_action.args == {"angle": 120}

    speech_action = reflex.actions[1]
    assert speech_action.action == "speak"
    assert "looking up" in speech_action.args["text"]


@pytest.mark.asyncio
async def test_fast_tool_router():
    from botgear.agent_surface.router import FastToolRouter

    router = FastToolRouter(max_candidates=2)
    catalog = {
        f"/device/dev_{i}/control::action_{i}": ToolDescriptor(
            topic=f"/device/dev_{i}/control", action=f"action_{i}", description=f"Tool {i}"
        )
        for i in range(20)
    }
    catalog["/tasmota/office/control::power_set"] = ToolDescriptor(
        topic="/tasmota/office/control", action="power_set", description="Turn power on or off"
    )

    filtered = await router.route_tools("turn power off", catalog)
    assert len(filtered) <= 2
    # Ensure relevant power tool was matched
    assert any("power_set" in t.action for t in filtered)


@pytest.mark.asyncio
async def test_fast_decision_on_start_publishes_state(monkeypatch):
    meta = ServiceMetadata(
        proxy_id="decision-1",
        service_meta_id="fast_decision@1.0.0",
        type_name="fast_decision",
        type_version="1.0.0",
        tags=[],
        singleton=False,
    )
    service = FastDecisionService(meta=meta, config={"residency": "local_only"})
    published = []
    monkeypatch.setattr(service, "publish", lambda suffix, payload, retained=False: published.append((suffix, payload, retained)))

    async def _no_loop():
        return None

    monkeypatch.setattr(service, "run_control_loop", _no_loop)
    await service.on_start()
    await service.on_stop()

    assert published == [("state", {
        "status": "ready",
        "residency": "local_only",
        "confidence_threshold": 0.95,
        "mode": "offline_mock",
    }, True)]


@pytest.mark.asyncio
async def test_fast_reflex_engine_prefers_power_set_over_toggle():
    # tasmota advertises both power_set and power_toggle on one topic;
    # "turn off" must set off, not toggle (which would turn an off light on).
    engine = FastReflexEngine()
    catalog = {
        "/tasmota/switch-1/control::power_set": ToolDescriptor(
            topic="/tasmota/switch-1/control", action="power_set"
        ),
        "/tasmota/switch-1/control::power_toggle": ToolDescriptor(
            topic="/tasmota/switch-1/control", action="power_toggle"
        ),
    }
    reflex = await engine.evaluate_utterance("turn off the switch", catalog)
    assert reflex is not None
    assert (reflex.actions[0].action, reflex.actions[0].args) == ("power_set", {"on": False})

    reflex = await engine.evaluate_utterance("toggle the switch", catalog)
    assert reflex is not None
    assert reflex.actions[0].action == "power_toggle"

    # A toggle-only device can't be driven to an absolute state.
    toggle_only = {k: v for k, v in catalog.items() if v.action == "power_toggle"}
    assert await engine.evaluate_utterance("turn off the switch", toggle_only) is None


@pytest.mark.asyncio
async def test_fast_reflex_engine_single_light_needs_no_name():
    engine = FastReflexEngine()
    one = {"/tasmota/switch-1/control::power_set": ToolDescriptor(
        topic="/tasmota/switch-1/control", action="power_set")}
    reflex = await engine.evaluate_utterance("turn the light off", one)
    assert reflex is not None
    assert (reflex.actions[0].topic, reflex.actions[0].args) == ("/tasmota/switch-1/control", {"on": False})

    # With two lights an unnamed target stays with System 2.
    two = dict(one)
    two["/tasmota/switch-2/control::power_set"] = ToolDescriptor(
        topic="/tasmota/switch-2/control", action="power_set")
    assert await engine.evaluate_utterance("turn the light off", two) is None


def test_env_key_alone_does_not_make_client_live(monkeypatch):
    # The brain reflex engine and tool router build a default client; an
    # ambient TYPESAFE_API_KEY must not send utterances off-host.
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-real-key")
    assert create_decision_client().mock is True
    assert FastReflexEngine().client.mock is True

    live = create_decision_client(mock=False)
    assert live.mock is False and live.api_key == "ts-real-key"
    assert create_decision_client(api_key="ts-explicit").mock is False


@pytest.mark.asyncio
async def test_remote_failure_falls_back_per_call_not_forever(monkeypatch):
    client = DecisionClient(api_key="ts-key", base_url="http://127.0.0.1:9")
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        raise ConnectionError("down")

    monkeypatch.setattr("rlx_decision.client.requests.post", fake_post)
    for _ in range(2):
        res = await client.decide_choice("turn off the light", "Target state?", ["on", "off"])
        assert res.choice == "off"  # mock fallback answered
    assert len(calls) == 2  # the second call retried the live API
