# unmanaged
"""brain with a session mind (roadmap Phase 4, ``claude_agent`` backend).

The engine keeps the runtime — listening, silence/echo re-listens, the
safety gate, speak_wait, the loop breaker, exit phrases — and hands the
session one turn per real utterance. A fake session stands in for the
Claude Agent SDK: each scripted turn makes tool calls through the
engine's handler, then ends with optional text.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

_BRAIN_PKG = Path(__file__).resolve().parents[1] / "repo" / "brain" / "1.0.0"
sys.path.insert(0, str(_BRAIN_PKG))

from brain.adapters.base import ModelAdapter  # noqa: E402
from brain.adapters.claude_agent import ClaudeAgentMind, TurnResult, _bind  # noqa: E402
from brain.schemas import (  # noqa: E402
    AllowedTools, ExitOnPhrase, LoopBreaker, LoopOnTimeout, ToolTerminationCondition, ToolDescriptor,
    ToolPattern, ToolResult, Workflow,
)
from brain.workflow_engine import WorkflowEngine  # noqa: E402

SPEECH = "/speech/mouth/control"
STT = "/stt/ear/control"
SWITCH = "/tasmota/office/control"
MOTOR = "/motor/m1/control"


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)
    (ws / "runs").mkdir()
    return ws


def _catalog() -> Dict[str, ToolDescriptor]:
    obj = {"type": "object", "properties": {}}
    descriptors = [
        ToolDescriptor(topic=SPEECH, action="speak", description="Speak aloud",
                       parameters={"type": "object", "properties": {"text": {"type": "string"}}}),
        ToolDescriptor(topic=STT, action="listen", description="Listen", parameters=obj),
        ToolDescriptor(topic=SWITCH, action="power_set", description="Relay on/off", parameters=obj),
        ToolDescriptor(topic=MOTOR, action="spin", description="Spin the motor", parameters=obj),
    ]
    return {f"{d.topic}::{d.action}": d for d in descriptors}


def _workflow(conversational: bool = True, **kw) -> Workflow:
    extra: Dict[str, Any] = {}
    if conversational:
        extra = dict(
            exit_on_phrase=ExitOnPhrase(matches=["goodbye"], listen_topic=["/stt/+/text"]),
            loop_on_timeout=LoopOnTimeout(tool="listen", field="timeout", value=True),
        )
    return Workflow(
        name="talk",
        max_steps=50,
        allowed_tools=AllowedTools(allowed=[
            ToolPattern(topic="/speech/*/control", actions=["speak"]),
            ToolPattern(topic="/stt/*/control", actions=["listen"]),
            ToolPattern(topic="/tasmota/*/control", actions=["power_set"]),
        ]),
        **{**extra, **kw},
    )


Call = Tuple[str, str, Dict[str, Any]]


class FakeSession(ModelAdapter):
    """Scripted session mind. ``turns``: one (calls, closing_text) per turn."""

    name = "claude_agent"
    is_session = True

    def __init__(self, turns: List[Tuple[List[Call], str]], error: Optional[str] = None):
        self.turns = list(turns)
        self.error = error
        self.messages: List[str] = []
        self.offered: List[str] = []
        self.system = ""
        self.handler_results: List[Tuple[str, bool]] = []
        self.closed = False

    async def complete(self, *a, **kw):  # pragma: no cover — never called for a session
        raise AssertionError("session minds are not called per step")

    async def open(self, *, system_prompt, tools, handler, model=None):
        self.system = system_prompt
        self.offered = [f"{t.topic}::{t.action}" for t in tools]
        self._by_key = {f"{t.topic}::{t.action}": t for t in tools}
        self.handler = handler

    async def turn(self, text: str) -> TurnResult:
        self.messages.append(text)
        if self.error:
            return TurnResult(is_error=True, error=self.error)
        calls, closing = self.turns.pop(0) if self.turns else ([], "")
        for topic, action, args in calls:
            desc = self._by_key.get(f"{topic}::{action}") or ToolDescriptor(topic=topic, action=action)
            self.handler_results.append(await self.handler(desc, args))
        return TurnResult(text=closing, tool_calls=len(calls), cost_usd=0.01,
                          input_tokens=100, output_tokens=10, api_turns=len(calls) + 1)

    async def close(self):
        self.closed = True


class _Tools:
    """Stub bus: ``listen`` replies from a script; when the script runs
    out, the operator says the exit phrase (the run ends cleanly)."""

    def __init__(self, heard: List[Dict[str, Any]]):
        self.heard = list(heard)
        self.calls: List[Call] = []
        self.engine: Optional[WorkflowEngine] = None

    async def __call__(self, topic: str, action: str, args: Dict[str, Any]) -> ToolResult:
        self.calls.append((topic, action, dict(args)))
        if action == "listen":
            if not self.heard:
                self.engine._exit_via_phrase = True
                self.engine._exit_phrase_matched = "goodbye"
                self.engine._run_task.cancel()
                await asyncio.sleep(10)
            return ToolResult(status="ok", value=self.heard.pop(0))
        return ToolResult(status="ok", value={"spoken": args.get("text")} if action == "speak" else {"ok": True})

    def actions(self) -> List[str]:
        return [a for _, a, _ in self.calls]


def _said(text: str) -> Dict[str, Any]:
    return {"text": text, "bearing": None, "timeout": False}


SILENCE = {"text": "", "bearing": None, "timeout": True}


def _engine(workspace, wf, mind, tools) -> WorkflowEngine:
    engine = WorkflowEngine(
        workflow=wf, workspace_dir=workspace, adapter=mind, tool_caller=tools,
        publish=lambda topic, payload, **kw: None, bus_prefix="/brain/brain-1",
        tool_catalog=_catalog(), inputs={"listen_window_seconds": 5},
    )
    tools.engine = engine
    return engine


# ─── conversation ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_engine_listens_and_session_acts_per_utterance(workspace):
    mind = FakeSession([
        ([(SWITCH, "power_set", {"on": False})], "Switch one is off."),
    ])
    tools = _Tools([SILENCE, SILENCE, _said("turn switch one off")])
    record = await _engine(workspace, _workflow(), mind, tools).run()

    assert record.status == "success" and "goodbye" in record.result_summary
    # Silence re-listened without a turn; one turn for the one utterance.
    assert len(mind.messages) == 1
    assert mind.messages[0].startswith("{'text': 'turn switch one off'")
    assert "[now:" in mind.messages[0]
    assert tools.actions() == ["listen", "listen", "listen", "power_set", "speak", "listen"]
    assert tools.calls[0][2] == {"timeout_seconds": 5}
    # The engine speaks the closing text, with speak_wait.
    assert tools.calls[4][2] == {"text": "Switch one is off.", "wait": True}
    # listen and the reply's speak are the engine's job: the session is
    # offered neither, nor anything the workflow doesn't allow.
    assert mind.offered == [f"{SWITCH}::power_set"]
    assert "no listen or speak tool" in mind.system
    assert mind.closed
    assert record.cost_usd == pytest.approx(0.01)
    assert record.steps_used == 2                  # power_set + the closing text: no speak round trip


@pytest.mark.asyncio
async def test_closing_text_is_the_spoken_reply(workspace):
    mind = FakeSession([([], "Hello there.")])
    tools = _Tools([_said("hi")])
    await _engine(workspace, _workflow(), mind, tools).run()
    assert (SPEECH, "speak", {"text": "Hello there.", "wait": True}) in tools.calls


@pytest.mark.asyncio
async def test_reply_speak_is_not_offered_so_a_turn_cannot_repeat_itself(workspace):
    # Live (Haiku, 2026-10-04): offered speak, the session ended turns on a
    # speak with no text; Claude Code injected "no visible output" and the
    # model spoke the time again, 4 times, until the loop breaker tripped.
    # It also paraphrased its own speak in the closing text (two answers).
    mind = FakeSession([([], "It's two fifty-eight in the afternoon.")])
    tools = _Tools([_said("what time is it")])
    await _engine(workspace, _workflow(), mind, tools).run()
    assert f"{SPEECH}::speak" not in mind.offered
    assert [a["text"] for _, act, a in tools.calls if act == "speak"] == [
        "It's two fifty-eight in the afternoon."]


@pytest.mark.asyncio
async def test_blocked_call_is_an_error_result_not_the_end(workspace):
    mind = FakeSession([
        ([(MOTOR, "spin", {})], "I can't spin that."),
    ])
    tools = _Tools([_said("spin the motor")])
    record = await _engine(workspace, _workflow(), mind, tools).run()
    text, is_error = mind.handler_results[0]
    assert is_error and "safety gate" in text
    assert "spin" not in tools.actions()
    assert record.status == "success"


@pytest.mark.asyncio
async def test_self_echo_is_dropped_without_a_turn(workspace):
    mind = FakeSession([([], "Switch one is off now."), ([], "")])
    tools = _Tools([_said("turn it off"), _said("switch one is off now"), _said("thanks")])
    await _engine(workspace, _workflow(), mind, tools).run()
    assert [m.split("'")[3] for m in mind.messages] == ["turn it off", "thanks"]


@pytest.mark.asyncio
async def test_turn_error_fails_the_run(workspace):
    mind = FakeSession([], error="error_max_budget_usd")
    record = await _engine(workspace, _workflow(), mind, _Tools([_said("hi")])).run()
    assert record.status == "failure" and "error_max_budget_usd" in record.failure_reason
    assert mind.closed


@pytest.mark.asyncio
async def test_loop_breaker_stops_a_runaway_turn(workspace):
    speaks = [(SPEECH, "speak", {"text": f"line {i}"}) for i in range(5)]
    mind = FakeSession([(speaks, "")])
    tools = _Tools([_said("talk")])
    wf = _workflow(loop_breaker=LoopBreaker(max_consecutive_speaks=3))
    record = await _engine(workspace, wf, mind, tools).run()
    assert record.status == "failure" and "loop breaker" in record.failure_reason
    assert tools.actions().count("speak") == 3              # the rest were refused
    assert mind.handler_results[-1] == ("ERROR the run has ended", True)


# ─── one-shot workflows ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_shot_workflow_is_one_turn(workspace):
    mind = FakeSession([([(SWITCH, "power_set", {"on": True})], "Switched on.")])
    tools = _Tools([])
    record = await _engine(workspace, _workflow(conversational=False), mind, tools).run()
    assert record.status == "success" and record.result_summary == "Switched on."
    assert len(mind.messages) == 1 and "listen" not in tools.actions()
    assert f"{STT}::listen" in mind.offered                  # not conversational: nothing withheld


@pytest.mark.asyncio
async def test_one_shot_needs_terminate_on(workspace):
    wf = _workflow(conversational=False, terminate_on=[ToolTerminationCondition(action="power_set")])
    record = await _engine(workspace, wf, FakeSession([([], "Done, I think.")]), _Tools([])).run()
    assert record.status == "failure" and "power_set" in record.failure_reason


@pytest.mark.asyncio
async def test_one_shot_terminate_on_succeeds_mid_turn(workspace):
    wf = _workflow(conversational=False, terminate_on=[ToolTerminationCondition(action="power_set")])
    mind = FakeSession([([(SWITCH, "power_set", {"on": True}), (SWITCH, "power_set", {"on": False})], "")])
    tools = _Tools([])
    record = await _engine(workspace, wf, mind, tools).run()
    assert record.status == "success" and "terminate_on" in record.result_summary
    assert tools.actions() == ["power_set"]


# ─── the SDK adapter's own pieces (no SDK session started) ─────────────


def test_tool_log_names_match_the_session(workspace):
    mind = ClaudeAgentMind()
    log = mind.encode_tools_for_log(list(_catalog().values()))
    assert [t["name"] for t in log] == [
        "mcp__botgear__speak", "mcp__botgear__listen", "mcp__botgear__power_set", "mcp__botgear__spin",
    ]
    assert log[0]["input_schema"]["properties"] == {"text": {"type": "string"}}


@pytest.mark.asyncio
async def test_handler_exception_becomes_an_error_result():
    async def boom(desc, args):
        raise RuntimeError("bus down")
    out = await _bind(boom, ToolDescriptor(topic=SWITCH, action="power_set"))({"on": True})
    assert out["is_error"] and "bus down" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_complete_is_not_used():
    with pytest.raises(NotImplementedError):
        await ClaudeAgentMind().complete([])
