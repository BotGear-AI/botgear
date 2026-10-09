# unmanaged
"""Brain-side defences against the robot answering its own voice.

Regression for a general_assistant run where ``speak`` returned on
enqueue, the next ``listen`` transcribed the speaker, and the model
replied to its own words in a loop (echo samples below are verbatim
from that run's tool_calls.jsonl). Two engine behaviours:

  * ``speak_wait`` — ``speak`` on /speech/*/control gets ``wait: true``
    so the call returns when the reply has finished playing.
  * ``echo_filter`` — a ``listen`` result that mostly repeats recent
    speech is dropped and re-listened; heard twice running, it passes.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_BRAIN_PKG = Path(__file__).resolve().parents[1] / "repo" / "brain" / "1.0.0"
sys.path.insert(0, str(_BRAIN_PKG))

from brain.adapters.mock import MockAdapter, script_call, script_done  # noqa: E402
from brain.context_loader import load_workflow  # noqa: E402
from brain.schemas import (  # noqa: E402
    AllowedTools, EchoFilter, LoopBreaker, LoopOnTimeout, ToolDescriptor, ToolPattern, ToolResult, Workflow,
)
from brain.workflow_engine import WorkflowEngine, _echo_containment, _echo_words  # noqa: E402

SPEECH = "/speech/mouth/control"
STT = "/stt/ear/control"
CHAT = "/chat/chat-1/control"

SPOKEN = "I didn't catch that. Could you clarify what you need help with?"
ECHO = "I DIDN'T CATCH THAT COULD YOU CLARIFY WHAT YOU NEED HELP WITH"


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)
    (ws / "runs").mkdir()
    return ws


def _catalog() -> Dict[str, ToolDescriptor]:
    descriptors = [
        ToolDescriptor(topic=SPEECH, action="speak", description="Speak aloud",
                       parameters={"type": "object", "properties": {"text": {"type": "string"}}}),
        ToolDescriptor(topic=STT, action="listen", description="Listen",
                       parameters={"type": "object", "properties": {}}),
        ToolDescriptor(topic=CHAT, action="speak", description="Send a chat message",
                       parameters={"type": "object", "properties": {"text": {"type": "string"}}}),
    ]
    return {f"{d.topic}::{d.action}": d for d in descriptors}


def _workflow(**kw) -> Workflow:
    return Workflow(
        name="echo",
        max_steps=10,
        allowed_tools=AllowedTools(allowed=[
            ToolPattern(topic="/speech/*/control", actions=["speak"]),
            ToolPattern(topic="/stt/*/control", actions=["listen"]),
            ToolPattern(topic="/chat/*/control", actions=["speak"]),
        ]),
        loop_on_timeout=LoopOnTimeout(tool="listen", field="timeout", value=True),
        **kw,
    )


class _Tools:
    """Stub tool caller: records every call; ``listen`` replies come from a
    script, ``speak`` echoes back the service's response shape."""

    def __init__(self, heard: List[Dict[str, Any]]):
        self.heard = list(heard)
        self.calls: List[tuple] = []

    async def __call__(self, topic: str, action: str, args: Dict[str, Any]) -> ToolResult:
        self.calls.append((topic, action, dict(args)))
        if action == "listen":
            return ToolResult(status="ok", value=self.heard.pop(0))
        return ToolResult(status="ok", value={"spoken": args.get("text"), "queued": 1})

    def listens(self) -> int:
        return sum(1 for _, a, _ in self.calls if a == "listen")


def _engine(workspace, wf, script, tools, published=None) -> WorkflowEngine:
    sink = published if published is not None else []
    return WorkflowEngine(
        workflow=wf,
        workspace_dir=workspace,
        adapter=MockAdapter(script=script),
        tool_caller=tools,
        publish=lambda topic, payload, **kw: sink.append((topic, payload)),
        bus_prefix="/brain/brain-1",
        tool_catalog=_catalog(),
    )


def _said(text: str) -> Dict[str, Any]:
    return {"text": text, "bearing": None, "timeout": False}


# ─── speak_wait ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_speak_on_speech_topic_waits_for_playback(workspace):
    tools = _Tools([])
    engine = _engine(workspace, _workflow(), [
        script_call(SPEECH, "speak", {"text": "hello"}),
        script_call(CHAT, "speak", {"text": "hello"}),
        script_call(SPEECH, "speak", {"text": "hi", "wait": False}),
        script_done("ok"),
    ], tools)
    await engine.run()
    speech_args = [args for topic, _, args in tools.calls]
    assert speech_args[0] == {"text": "hello", "wait": True}      # speech: injected
    assert speech_args[1] == {"text": "hello"}                    # chat channel: untouched
    assert speech_args[2] == {"text": "hi", "wait": False}        # explicit choice respected


@pytest.mark.asyncio
async def test_speak_wait_can_be_disabled(workspace):
    tools = _Tools([])
    engine = _engine(workspace, _workflow(speak_wait=False), [
        script_call(SPEECH, "speak", {"text": "hello"}), script_done("ok"),
    ], tools)
    await engine.run()
    assert tools.calls[0][2] == {"text": "hello"}


# ─── echo_filter ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_listen_drops_own_speech_and_relistens(workspace):
    tools = _Tools([_said(ECHO), _said("turn on switch one")])
    engine = _engine(workspace, _workflow(), [
        script_call(SPEECH, "speak", {"text": SPOKEN}),
        script_call(STT, "listen", {}),
        script_done("ok"),
    ], tools)
    await engine.run()
    assert tools.listens() == 2                         # echo dropped, listened again
    tool_calls = (engine.run_dir / "tool_calls.jsonl").read_text()
    assert "turn on switch one" in tool_calls           # the real reply came through


@pytest.mark.asyncio
async def test_operator_repeating_robot_passes_on_second_try(workspace):
    tools = _Tools([_said(ECHO), _said(ECHO)])
    engine = _engine(workspace, _workflow(), [
        script_call(SPEECH, "speak", {"text": SPOKEN}),
        script_call(STT, "listen", {}),
        script_done("ok"),
    ], tools)
    await engine.run()
    assert tools.listens() == 2                         # second identical hearing passes


@pytest.mark.asyncio
async def test_short_replies_are_never_filtered(workspace):
    tools = _Tools([_said("yes")])
    engine = _engine(workspace, _workflow(), [
        script_call(SPEECH, "speak", {"text": "Should I say yes?"}),
        script_call(STT, "listen", {}),
        script_done("ok"),
    ], tools)
    await engine.run()
    assert tools.listens() == 1


@pytest.mark.asyncio
async def test_unrelated_reply_passes(workspace):
    tools = _Tools([_said("what devices are on the network")])
    engine = _engine(workspace, _workflow(), [
        script_call(SPEECH, "speak", {"text": SPOKEN}),
        script_call(STT, "listen", {}),
        script_done("ok"),
    ], tools)
    await engine.run()
    assert tools.listens() == 1


@pytest.mark.asyncio
async def test_echo_filter_disabled(workspace):
    tools = _Tools([_said(ECHO)])
    engine = _engine(workspace, _workflow(echo_filter=EchoFilter(enabled=False)), [
        script_call(SPEECH, "speak", {"text": SPOKEN}),
        script_call(STT, "listen", {}),
        script_done("ok"),
    ], tools)
    await engine.run()
    assert tools.listens() == 1


@pytest.mark.asyncio
async def test_echo_outside_window_passes(workspace, monkeypatch):
    tools = _Tools([_said(ECHO)])
    engine = _engine(workspace, _workflow(echo_filter=EchoFilter(window_seconds=0.0)), [
        script_call(SPEECH, "speak", {"text": SPOKEN}),
        script_call(STT, "listen", {}),
        script_done("ok"),
    ], tools)
    await engine.run()
    assert tools.listens() == 1


@pytest.mark.parametrize("heard, spoken", [
    (ECHO, SPOKEN),
    ("HERE TO HELP COULD YOU PLEASE CLARIFY WHAT YOU NEED ASSISTANCE WITH FOR EXAMPLE "
     "YOU CAN ASK ABOUT DEVICE STATUAL SWITCHES MANAGE AUDIO SETTINGS OR ANYTHING ELSE YOU NEED",
     "I'm here to help! Could you please clarify what you need assistance with? For example, "
     "you can ask about device status, control switches, manage audio settings, or anything else you need."),
])
def test_real_echoes_from_the_loop_score_high(heard, spoken):
    assert _echo_containment(_echo_words(heard), _echo_words(spoken)) >= EchoFilter().threshold


# ─── loader ─────────────────────────────────────────────────────────────


def _write_wf(workspace, name, extra_yaml):
    d = workspace / "workflows" / name
    d.mkdir(parents=True)
    (d / "workflow.yaml").write_text(f"name: {name}\npreferred_backend: mock\n{extra_yaml}")
    (d / "prompt.md").write_text("p")
    (d / "allowed_tools.yaml").write_text("allowed: []\nblocked: []\n")


def test_loader_defaults_and_overrides(workspace):
    _write_wf(workspace, "plain", "")
    _write_wf(workspace, "tuned", "speak_wait: false\necho_filter:\n  window_seconds: 5\n  threshold: 0.9\n")
    _write_wf(workspace, "off", "echo_filter: false\n")

    plain = load_workflow("plain", workspace)
    assert plain.speak_wait is True and plain.echo_filter == EchoFilter()
    tuned = load_workflow("tuned", workspace)
    assert tuned.speak_wait is False
    assert tuned.echo_filter.window_seconds == 5 and tuned.echo_filter.threshold == 0.9
    assert tuned.echo_filter.enabled is True
    assert load_workflow("off", workspace).echo_filter.enabled is False


# ─── loop breaker + end-of-run silencing ─────────────────────────────────


def _stops(published) -> List[str]:
    return [t for t, p in published if isinstance(p, dict) and p.get("action") == "stop"]


@pytest.mark.asyncio
async def test_loop_breaker_trips_on_consecutive_speaks_and_silences(workspace):
    published: list = []
    tools = _Tools([])
    engine = _engine(workspace, _workflow(), [
        script_call(SPEECH, "speak", {"text": f"reply {i}"}) for i in range(6)
    ] + [script_done("ok")], tools, published)
    record = await engine.run()
    assert record.status == "failure"
    assert "loop breaker" in record.failure_reason and "spoke 4 times" in record.failure_reason
    assert sum(1 for _, a, _ in tools.calls if a == "speak") == 4
    assert _stops(published) == [SPEECH]               # queued replies cut off


@pytest.mark.asyncio
async def test_hearing_the_operator_resets_the_speak_count(workspace):
    tools = _Tools([_said("what devices are online"), _said("turn on switch one")])
    script = []
    for _ in range(2):
        script += [script_call(SPEECH, "speak", {"text": "a"}), script_call(SPEECH, "speak", {"text": "b"}),
                   script_call(SPEECH, "speak", {"text": "c"}), script_call(STT, "listen", {})]
    engine = _engine(workspace, _workflow(), script + [script_done("ok")], tools)
    record = await engine.run()
    assert record.status == "success"                  # 3 speaks, heard, 3 speaks, heard — fine


@pytest.mark.asyncio
async def test_loop_breaker_trips_on_repeated_echoes(workspace):
    # Each echo is a different sentence the robot said, so the
    # "same thing twice = operator" escape doesn't apply.
    lines = ["I didn't catch that could you clarify", "here to help please clarify what you need",
             "ready to assist with any of those areas"]
    heard = []
    for x in lines:
        heard += [_said(x.upper()), _said("um okay sure thing")]   # echo (dropped), then the operator
    tools = _Tools(heard)
    script = []
    for x in lines:
        script += [script_call(SPEECH, "speak", {"text": x}), script_call(STT, "listen", {})]
    engine = _engine(workspace, _workflow(loop_breaker=LoopBreaker(max_consecutive_speaks=0)),
                     script + [script_done("ok")], tools)
    record = await engine.run()
    assert record.status == "failure" and "heard its own voice 3 times" in record.failure_reason


@pytest.mark.asyncio
async def test_loop_breaker_disabled(workspace):
    tools = _Tools([])
    engine = _engine(workspace, _workflow(loop_breaker=LoopBreaker(enabled=False)), [
        script_call(SPEECH, "speak", {"text": f"reply {i}"}) for i in range(6)
    ] + [script_done("ok")], tools)
    assert (await engine.run()).status == "success"


@pytest.mark.asyncio
async def test_normal_done_lets_the_last_reply_finish(workspace):
    published: list = []
    engine = _engine(workspace, _workflow(), [
        script_call(SPEECH, "speak", {"text": "bye"}), script_done("ok"),
    ], _Tools([]), published)
    assert (await engine.run()).status == "success"
    assert _stops(published) == []


@pytest.mark.asyncio
async def test_cancel_silences_speech(workspace):
    published: list = []

    class _SlowTools(_Tools):
        async def __call__(self, topic, action, args):
            if action == "listen":
                await asyncio.sleep(10)
            return await super().__call__(topic, action, args)

    engine = _engine(workspace, _workflow(), [
        script_call(SPEECH, "speak", {"text": "hi"}), script_call(STT, "listen", {}), script_done("ok"),
    ], _SlowTools([]), published)
    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.05)
    engine.cancel("halted by voice:ear: heard 'stop'")
    task.cancel()
    record = await task
    assert record.status == "cancelled"
    assert record.failure_reason == "halted by voice:ear: heard 'stop'"
    assert _stops(published) == [SPEECH]


def test_loader_parses_loop_breaker(workspace):
    _write_wf(workspace, "lb", "loop_breaker:\n  max_consecutive_speaks: 6\n")
    _write_wf(workspace, "lboff", "loop_breaker: false\n")
    assert load_workflow("lb", workspace).loop_breaker.max_consecutive_speaks == 6
    assert load_workflow("lboff", workspace).loop_breaker.enabled is False


@pytest.mark.parametrize("heard, spoken", [
    # The operator repeating a light command right after the robot spoke
    # about the light (observed live: both dropped as echo at exactly 0.75).
    ("TURN ON THE LIGHT", "turning on the light"),
    ("TURN ON THE LIGHT", "I'm not sure what you mean by 'PENAL'. Could you clarify "
                          "if you'd like to turn the light off or on?"),
])
def test_short_commands_are_not_echoes_of_similar_speech(heard, spoken):
    assert _echo_containment(_echo_words(heard), _echo_words(spoken)) < EchoFilter().threshold
