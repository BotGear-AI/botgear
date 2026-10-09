# unmanaged
"""Conversation-quality behaviours of the brain engine, from a
general_assistant session where "turn switch one off" got "Turning it
off now" → "I can't control the switch" → (discovers) → "turned off":

  * resume_on_done — a mid-conversation ``done`` speaks its text (small
    models put the reply there) and listens again, same run + context.
  * lazy_tools_threshold — small catalogs load everything up front.
  * discovered_prefixes — capabilities loaded by earlier runs stay loaded.
  * refusal check — "I can't …" is held back once while a live
    capability is still unloaded, with a pointer to describe_capability.
  * runtime environment — run header carries time/version/host facts,
    heard utterances get a ``[now: …]`` stamp (stamp_time).
  * describe_capability results are gated like the tool list, and the
    capability index in context is rewritten after a discovery.
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
from brain.schemas import (  # noqa: E402
    AllowedTools, ExitOnPhrase, LoopOnTimeout, ToolDescriptor, ToolResult, Workflow,
)
from brain.workflow_engine import WorkflowEngine  # noqa: E402

SPEECH = "/speech/mouth/control"
STT = "/stt/ear/control"
SWITCH = "/tasmota/switch-1/control"


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)
    (ws / "runs").mkdir()
    return ws


def _catalog() -> Dict[str, ToolDescriptor]:
    obj = {"type": "object", "properties": {}}
    descriptors = [
        ToolDescriptor(topic=SPEECH, action="speak", description="Speak", parameters=obj),
        ToolDescriptor(topic=STT, action="listen", description="Listen", parameters=obj),
        ToolDescriptor(topic=SWITCH, action="power_set", description="Switch a relay", parameters=obj),
        ToolDescriptor(topic="/brain/brain-1/control", action="describe_capability",
                       description="Load a capability", parameters=obj),
    ]
    return {f"{d.topic}::{d.action}": d for d in descriptors}


def _wf(**kw) -> Workflow:
    base = dict(
        name="chat",
        max_steps=6,
        timeout_seconds=2,                 # the script-exhausted tail just listens until this
        allowed_tools=AllowedTools(allowed=[], blocked=[], default_policy="allow"),
        loop_on_timeout=LoopOnTimeout(tool="listen", field="timeout", value=True),
        exit_on_phrase=ExitOnPhrase(matches=["goodbye"], listen_topic="/stt/+/text"),
        lazy_tools=True,
        always_loaded_prefixes=["stt", "speech"],
    )
    base.update(kw)
    return Workflow(**base)


class _Tools:
    def __init__(self, heard: List[str]):
        self.heard = list(heard)
        self.calls: List[tuple] = []

    async def __call__(self, topic: str, action: str, args: Dict[str, Any]) -> ToolResult:
        self.calls.append((topic, action, dict(args)))
        if action == "listen":
            if not self.heard:
                await asyncio.sleep(0.01)          # a real listen blocks for its window
                return ToolResult(status="ok", value={"text": "", "timeout": True})
            return ToolResult(status="ok", value={"text": self.heard.pop(0), "timeout": False})
        if action == "speak":
            return ToolResult(status="ok", value={"spoken": args.get("text"), "queued": 1})
        return ToolResult(status="ok", value={"ok": True})

    def said(self) -> List[str]:
        return [a["text"] for _, act, a in self.calls if act == "speak"]

    def count(self, action: str) -> int:
        return sum(1 for _, a, _ in self.calls if a == action)


def _engine(workspace, wf, script, tools, **kw) -> WorkflowEngine:
    return WorkflowEngine(
        workflow=wf, workspace_dir=workspace, adapter=MockAdapter(script=script),
        tool_caller=tools, publish=lambda *a, **k: None, bus_prefix="/brain/brain-1",
        tool_catalog=_catalog(), **kw,
    )


# ─── resume_on_done ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_done_mid_conversation_speaks_reply_and_listens_again(workspace):
    tools = _Tools(["what voices do you have", "thanks"])
    engine = _engine(workspace, _wf(max_steps=4), [
        script_call(STT, "listen", {}),
        script_call(SPEECH, "speak", {"text": "Earlier reply."}),
        script_done("I have Amy and Alan."),          # reply put in done, not speak
    ], tools)
    record = await engine.run()
    assert "I have Amy and Alan." in tools.said()          # the unspoken reply was spoken
    assert tools.heard == []                                # …went back to listening and heard "thanks"
    last_msgs = engine.adapter.calls[-1]["messages"] if hasattr(engine.adapter, "calls") else []
    assert any("thanks" in m["content"] for m in last_msgs)  # same run kept the conversation
    assert record.status == "failure" and "done" not in (record.failure_reason or "")   # never ended by done


@pytest.mark.asyncio
async def test_done_text_already_spoken_is_not_repeated(workspace):
    tools = _Tools(["turn switch one on", "ok"])
    engine = _engine(workspace, _wf(max_steps=3), [
        script_call(STT, "listen", {}),
        script_call(SPEECH, "speak", {"text": "Switch one is now turned on."}),
        script_done("Switch one is now turned on."),
    ], tools)
    await engine.run()
    assert tools.said().count("Switch one is now turned on.") == 1
    assert tools.count("listen") == 2


@pytest.mark.asyncio
async def test_done_before_any_listen_still_fails(workspace):
    engine = _engine(workspace, _wf(), [script_done("bye")], _Tools([]))
    record = await engine.run()
    assert record.status == "failure" and "exit phrase" in record.failure_reason


@pytest.mark.asyncio
async def test_resume_on_done_can_be_disabled(workspace):
    tools = _Tools(["hello"])
    engine = _engine(workspace, _wf(resume_on_done=False), [
        script_call(STT, "listen", {}), script_done("Hi there."),
    ], tools)
    record = await engine.run()
    assert record.status == "failure" and tools.count("listen") == 1


# ─── lazy threshold + remembered discovery ──────────────────────────────


def test_small_catalog_loads_everything(workspace):
    engine = _engine(workspace, _wf(), [], _Tools([]))          # 4 tools ≤ 120
    topics = {d.topic for d in engine._lazy_tool_descriptors()}
    assert SWITCH in topics
    assert engine._capability_index_text() == ""
    assert engine._hidden_capabilities() == []


def test_large_catalog_stays_lazy(workspace):
    engine = _engine(workspace, _wf(lazy_tools_threshold=2), [], _Tools([]))
    assert SWITCH not in {d.topic for d in engine._lazy_tool_descriptors()}
    assert engine._hidden_capabilities() == ["tasmota"]


def test_capabilities_discovered_earlier_are_preloaded(workspace):
    engine = _engine(workspace, _wf(lazy_tools_threshold=2), [], _Tools([]),
                     discovered_prefixes={"tasmota"})
    assert SWITCH in {d.topic for d in engine._lazy_tool_descriptors()}
    assert engine._hidden_capabilities() == []


@pytest.mark.asyncio
async def test_brain_service_remembers_discovery_across_runs():
    from types import SimpleNamespace
    from brain.service import BrainService

    class _Eng:
        workflow = SimpleNamespace(name="general_assistant")
        _discovered_prefixes = {"tasmota"}
        _exit_via_phrase = False

        async def run(self):
            return None

    async def _noop():
        return None

    fake = SimpleNamespace(_runs={}, _run_tasks={}, _keepalive={}, _discovered={},
                           _publish_state=_noop)
    await BrainService._run_to_completion(fake, "r1", _Eng())
    assert fake._discovered == {"general_assistant": {"tasmota"}}


# ─── refusal check ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refusal_held_back_while_a_capability_is_unloaded(workspace):
    tools = _Tools(["turn switch one off"])
    engine = _engine(workspace, _wf(lazy_tools_threshold=2, max_steps=4), [
        script_call(STT, "listen", {}),
        script_call(SPEECH, "speak", {"text": "I cannot control the switch, the tool isn't available."}),
        script_call(SPEECH, "speak", {"text": "Sorry, I really can't do that."}),
    ], tools)
    await engine.run()
    said = tools.said()
    assert "I cannot control the switch, the tool isn't available." not in said   # held back, once
    assert "Sorry, I really can't do that." in said                              # not a loop
    nudge = [m for m in engine.adapter.calls[2]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "NOT SPOKEN" in nudge and "tasmota" in nudge


@pytest.mark.asyncio
async def test_refusal_spoken_when_nothing_is_hidden(workspace):
    tools = _Tools(["fly to the moon"])
    engine = _engine(workspace, _wf(max_steps=3), [
        script_call(STT, "listen", {}),
        script_call(SPEECH, "speak", {"text": "I can't fly, sorry."}),
    ], tools)
    await engine.run()
    assert "I can't fly, sorry." in tools.said()


# ─── runtime environment + clock ────────────────────────────────────────


@pytest.mark.asyncio
async def test_context_carries_environment_from_runtime_facts(workspace):
    engine = _engine(workspace, _wf(max_steps=1), [script_call(STT, "listen", {})], _Tools([]),
                     runtime_facts=lambda: {"botgear_version": "0.1.42", "hostname": "droid"})
    await engine.run()
    context = engine.adapter.calls[0]["messages"][1]["content"]
    assert "# Environment" in context
    assert "local time at run start:" in context and "timezone:" in context
    assert "- botgear_version: 0.1.42" in context and "- hostname: droid" in context


@pytest.mark.asyncio
async def test_broken_runtime_facts_do_not_fail_the_run(workspace):
    def _boom():
        raise RuntimeError("bus down")
    engine = _engine(workspace, _wf(max_steps=1), [script_call(STT, "listen", {})], _Tools([]),
                     runtime_facts=_boom)
    await engine.run()
    assert "local time at run start:" in engine.adapter.calls[0]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_heard_utterance_is_stamped_with_current_time(workspace):
    tools = _Tools(["what time is it"])
    engine = _engine(workspace, _wf(max_steps=2), [
        script_call(STT, "listen", {}),
        script_call(SPEECH, "speak", {"text": "It's noon."}),
    ], tools)
    await engine.run()
    heard = [m for m in engine.adapter.calls[1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "what time is it" in heard and "[now: " in heard


@pytest.mark.asyncio
async def test_stamp_time_can_be_disabled(workspace):
    tools = _Tools(["what time is it"])
    engine = _engine(workspace, _wf(max_steps=2, stamp_time=False), [
        script_call(STT, "listen", {}),
        script_call(SPEECH, "speak", {"text": "No idea."}),
    ], tools)
    await engine.run()
    heard = [m for m in engine.adapter.calls[1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "[now: " not in heard


# ─── describe_capability gating + index refresh ─────────────────────────


class _DescribeTools(_Tools):
    """describe_capability answers like BrainService.m_describe_capability —
    every live tool under the prefix, blocked or not."""

    async def __call__(self, topic, action, args):
        if action == "describe_capability":
            self.calls.append((topic, action, dict(args)))
            actions = [
                {"topic": SWITCH, "action": "power_set", "description": "", "parameters": {}},
                {"topic": SWITCH, "action": "factory_reset", "description": "", "parameters": {}},
            ]
            return ToolResult(status="ok", value={
                "capability": args.get("capability"), "topics": [SWITCH], "actions": actions,
            })
        return await super().__call__(topic, action, args)


def _gated_wf(**kw) -> Workflow:
    from brain.schemas import ToolPattern
    return _wf(
        lazy_tools_threshold=2,
        allowed_tools=AllowedTools(
            allowed=[], default_policy="allow",
            blocked=[ToolPattern(topic="/tasmota/*/control", actions=["factory_reset"])],
        ),
        **kw,
    )


@pytest.mark.asyncio
async def test_describe_capability_hides_blocked_actions(workspace):
    engine = _engine(workspace, _gated_wf(max_steps=2), [
        script_call("/brain/brain-1/control", "describe_capability", {"capability": "tasmota"}),
        script_call(STT, "listen", {}),
    ], _DescribeTools([]))
    await engine.run()
    described = [m for m in engine.adapter.calls[1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "power_set" in described and "factory_reset" not in described


@pytest.mark.asyncio
async def test_capability_index_rewritten_after_discovery(workspace):
    engine = _engine(workspace, _gated_wf(max_steps=2), [
        script_call("/brain/brain-1/control", "describe_capability", {"capability": "tasmota"}),
        script_call(STT, "listen", {}),
    ], _DescribeTools([]))
    await engine.run()
    assert "tasmota" in engine.adapter.calls[0]["messages"][1]["content"]
    assert "Other live capabilities" not in engine.adapter.calls[1]["messages"][1]["content"]


def test_runtime_facts_read_from_retained_topics(monkeypatch):
    # Through a real Bus, not a stubbed reader — a stub hid that
    # Bus.retained_get didn't exist.
    import brain.service as svc
    from botgear.runtime.bus import Bus
    bus = Bus()
    for topic, payload in {
        "/runtime/info": {"id": "droid-1", "version": "0.0.0", "started_at": "2026-10-03T20:00:00+00:00"},
        "/runtime/runtime/state": {"os": {"name": "Linux", "version": "24.04", "arch": "x86_64",
                                          "hostname": "droid"}},
        "/runtime/runtime/power": {"battery": {"percent": 81.6, "status": "discharging"}},
        "/runtime/runtime/services": {"services": [{"status": "running"}, {"status": "stopped"}]},
    }.items():
        bus.publish_sync(topic, payload, retained=True)
    monkeypatch.setattr(svc, "get_bus", lambda: bus)
    monkeypatch.setattr(svc, "_git_sha", lambda: "abc1234")
    facts = svc._runtime_facts()
    assert facts == {
        "botgear_version": "dev build", "git_commit": "abc1234", "runtime_id": "droid-1",
        "runtime_started_at": "2026-10-03T20:00:00+00:00", "hostname": "droid",
        "os": "Linux 24.04 x86_64", "battery": "82% (discharging)", "running_services": 1,
    }


def test_runtime_facts_empty_bus(monkeypatch):
    import brain.service as svc
    monkeypatch.setattr(svc, "_retained", lambda topic: None)
    monkeypatch.setattr(svc, "_git_sha", lambda: "")
    assert svc._runtime_facts() == {}


# ─── live phase for the UI ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_phase_tracks_what_the_run_is_doing(workspace):
    seen: List[tuple] = []
    tools = _Tools(["turn switch one off"])
    engine = _engine(workspace, _wf(max_steps=3), [
        script_call(STT, "listen", {}),
        script_call(SWITCH, "power_set", {"on": False}),
    ], tools, on_change=lambda: seen.append((engine.phase, engine.phase_detail)))
    await engine.run()
    phases = [p for p, _ in seen]
    for expected in ("starting", "thinking", "listening", "calling"):
        assert expected in phases, (expected, phases)
    assert ("calling", "power_set") in seen
    summary = engine.live_summary()
    assert summary["last_heard"] == "turn switch one off" and summary["last_heard_at"]
    assert summary["steps_used"] >= 2 and summary["tool_calls"] >= 2


@pytest.mark.asyncio
async def test_silent_listens_are_counted_not_invisible(workspace):
    tools = _Tools([])                                   # nobody talks
    engine = _engine(workspace, _wf(timeout_seconds=1), [script_call(STT, "listen", {})], tools)
    await engine.run()
    assert engine.phase == "listening"
    assert engine.live_summary()["silent_listens"] > 5
    assert engine.live_summary()["last_heard"] is None


def test_brain_marks_runs_left_running_by_a_restart_interrupted(tmp_path):
    import json
    from types import SimpleNamespace
    from brain.service import BrainService
    runs = tmp_path / "runs"
    for name, status in [("a-run1", "running"), ("b-run2", "pending"), ("c-run3", "success")]:
        (runs / name).mkdir(parents=True)
        (runs / name / "summary.json").write_text(json.dumps({"run_id": name, "status": status}))
    fake = SimpleNamespace(_workspace=tmp_path, proxy_id="brain")
    BrainService._close_interrupted_runs(fake)
    status = {n: json.loads((runs / n / "summary.json").read_text()) for n in ("a-run1", "b-run2", "c-run3")}
    assert status["a-run1"]["status"] == status["b-run2"]["status"] == "interrupted"
    assert status["a-run1"]["ended_at"] and "backend stopped" in status["a-run1"]["failure_reason"]
    assert status["c-run3"]["status"] == "success"


def test_active_run_summary_carries_live_fields():
    from brain.service import ActiveRunSummary
    s = ActiveRunSummary(run_id="r", status="running", workflow="w", backend="ollama", model_id="m",
                         phase="listening", silent_listens=3, last_heard="hi")
    assert s.model_dump()["phase"] == "listening" and s.silent_listens == 3


@pytest.mark.asyncio
async def test_get_run_log_backfills_steps_and_tool_calls(tmp_path):
    import json
    from types import SimpleNamespace
    from brain.service import BrainService
    run = tmp_path / "runs" / "2026-10-04T13-45-08-general_assistant-f2f85a4e"
    run.mkdir(parents=True)
    (run / "steps.jsonl").write_text("\n".join(json.dumps({"step": i}) for i in range(5)) + "\n")
    (run / "tool_calls.jsonl").write_text("\n".join(json.dumps({"action": "listen", "n": i}) for i in range(300)) + "\nnot json\n")
    (run / "summary.json").write_text(json.dumps({"status": "running"}))
    fake = SimpleNamespace(_workspace=tmp_path)
    out = await BrainService.m_get_run_log(fake, run_id="f2f85a4e", limit=50)
    assert [s["step"] for s in out["steps"]] == [0, 1, 2, 3, 4]
    assert len(out["tool_calls"]) == 49 and out["tool_calls"][-1]["n"] == 299   # bad line skipped
    assert out["summary"]["status"] == "running"
    missing = await BrainService.m_get_run_log(fake, run_id="nope")
    assert "error" in missing and missing["steps"] == []


# ─── fast_reflex ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fast_reflex_actuates_without_the_model(workspace):
    tools = _Tools(["turn the light off"])
    script = [script_call(STT, "listen", {})]
    engine = _engine(workspace, _wf(max_steps=3, fast_reflex=True), script, tools)
    await engine.run()
    assert (SWITCH, "power_set", {"on": False}) in tools.calls
    assert "turning off the light" in tools.said()


@pytest.mark.asyncio
async def test_fast_reflex_is_off_unless_the_workflow_opts_in(workspace):
    tools = _Tools(["turn the light off"])
    engine = _engine(workspace, _wf(max_steps=3), [script_call(STT, "listen", {})], tools)
    await engine.run()
    assert tools.count("power_set") == 0


@pytest.mark.asyncio
async def test_fast_reflex_ignores_the_workflow_prompt(workspace):
    # conversation_session's prompt says "turn the switch on or off" — that
    # is an instruction to the model, not an operator command.
    tools = _Tools([])
    wf = _wf(max_steps=2, fast_reflex=True, prompt_md="If asked, turn the switch light on or off.")
    engine = _engine(workspace, wf, [script_call(STT, "listen", {})], tools)
    await engine.run()
    assert tools.count("power_set") == 0
