# unmanaged
"""agent service (repo/agent/1.0.0/agent_runner.py): runs agent_harness
workflows from the bus. Driven with scripted harness drivers (no cloud
agent), a fake bus, and the real harness pipeline (workspace, checks,
result.json)."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agent_harness import register_driver
from agent_harness.events import AgentEvent
from agent_harness.runtime import Capabilities

from botgear.framework.service import ServiceMetadata

_PATH = Path(__file__).resolve().parents[1] / "repo" / "agent" / "1.0.0" / "agent_runner.py"
_spec = importlib.util.spec_from_file_location("agent_runner_under_test", _PATH)
agent_runner = importlib.util.module_from_spec(_spec)
sys.modules["agent_runner_under_test"] = agent_runner
_spec.loader.exec_module(agent_runner)


class FakeBus:
    def __init__(self) -> None:
        self.published: List[tuple] = []

    def publish_sync(self, topic, payload, *, retained=False, **_):
        self.published.append((topic, payload, retained))
        return 1

    async def subscribe(self, topic, subscriber_id=None):
        await asyncio.Event().wait()
        yield None

    def on(self, suffix: str) -> List[Any]:
        return [p for t, p, _ in self.published if t.endswith(suffix)]


SEEN: Dict[str, Any] = {}


class ScriptedAgent:
    """Writes the declared outputs; optional slowness and an MCP capability."""
    name = "scripted"

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.capabilities = Capabilities(tools=True, streaming=True, resume=False, cloud=False, mcp=True)
        self._stop = threading.Event()

    def run(self, workspace: Path, task, policy):
        SEEN["task"] = task
        yield AgentEvent("status", self.name, {"phase": "init", "model": "scripted-1"})
        yield AgentEvent("tool_call", self.name, {"tool": "mcp__botgear__botgear_list_capabilities"})
        yield AgentEvent("message", self.name, {"text": "secret is " + ",".join(task.env.values())})
        if SEEN.get("slow"):
            self._stop.wait(10)
        for rel in task.outputs:
            p = workspace / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("All 3 services look healthy.\nok\n" if rel.endswith(".md") else "{}")
        yield AgentEvent("completed", self.name, {"message": "done"})

    def cancel(self) -> None:
        self._stop.set()


register_driver("scripted", ScriptedAgent)


SIMPLE = """---
name: simple
description: A tiny workflow.
outputs: [output/report.md]
---
Write the report.
"""


@pytest.fixture
def bus(monkeypatch):
    fake = FakeBus()
    monkeypatch.setattr("botgear.framework.service.get_bus", lambda: fake)
    monkeypatch.setattr("botgear.runtime.bus.get_bus", lambda: fake)
    from botgear.runtime import halt
    monkeypatch.setattr(halt, "is_halted", lambda: False)
    SEEN.clear()
    return fake


async def _service(tmp_path, **config) -> Any:
    ws = tmp_path / "agent-ws"
    (ws / "workflows" / "simple").mkdir(parents=True)
    (ws / "workflows" / "simple" / "WORKFLOW.md").write_text(SIMPLE)
    cfg = {"workspace_path": str(ws), "default_agent": "scripted", **config}
    svc = agent_runner.AgentService(ServiceMetadata(
        proxy_id="agent", service_meta_id="agent@1.0.0", type_name="agent", type_version="1.0.0",
        tags=[], singleton=False), cfg)
    await svc.on_start()
    return svc


@pytest.mark.asyncio
async def test_lists_bundled_and_instance_workflows(bus, tmp_path):
    svc = await _service(tmp_path)
    try:
        wfs = {w["name"]: w for w in await svc.m_list_workflows()}
        assert {"simple", "robot-health"} <= set(wfs)
        rh = wfs["robot-health"]
        assert rh["inputs"]["endpoint"]["auto"] and rh["inputs"]["ca_cert"]["auto"]
        assert rh["mcp"] == {"botgear": ["botgear_list_capabilities", "botgear_get_state"]}
    finally:
        await svc.on_stop()


@pytest.mark.asyncio
async def test_run_completes_publishes_result_and_announces(bus, tmp_path):
    svc = await _service(tmp_path, announce_topic="/speech/mouth/control")
    try:
        started = await svc.m_start_run("simple")
        assert started["status"] == "running"
        done = await svc.m_wait_run(started["run_id"], timeout_seconds=10)
        assert done["status"] == "completed", done
        assert done["deliverables"]["output/report.md"].startswith("All 3 services")
        result = bus.on(f"/runs/{started['run_id']}/result")[-1]
        assert result["status"] == "completed" and result["checks_total"] == 1
        speak = [p for t, p, _ in bus.published if t == "/speech/mouth/control"]
        assert speak and speak[0]["text"].startswith("The simple run finished. All 3 services look healthy")
        events = bus.on(f"/runs/{started['run_id']}/events")
        assert any(e["type"] == "tool_call" for e in events)
        runs = await svc.m_list_runs()
        assert runs[0]["run_id"] == started["run_id"] and runs[0]["status"] == "completed"
        log = await svc.m_get_run_log(started["run_id"])
        assert log["checks"] and any(e["type"] == "completed" for e in log["events"])
    finally:
        await svc.on_stop()


@pytest.mark.asyncio
async def test_robot_health_gets_endpoint_cert_and_key(bus, tmp_path):
    ca = tmp_path / "cert.pem"
    ca.write_text("PEM")
    svc = await _service(tmp_path, mcp_api_key="k-123456", ca_cert=str(ca),
                         mcp_endpoint="https://robot.local:8998/v1/mcp")
    try:
        no_key = await (await _service(tmp_path / "b")).m_start_run("robot-health")
        assert "mcp_api_key" in no_key["error"]
        started = await svc.m_start_run("robot-health")
        done = await svc.m_wait_run(started["run_id"], timeout_seconds=15)
        task = SEEN["task"]
        assert task.env == {"BOTGEAR_MCP_KEY": "k-123456"}
        assert task.mcp_servers[0].url == "https://robot.local:8998/v1/mcp"
        assert task.mcp_servers[0].ca_cert == str(ca.resolve())
        # The real check can't reach a robot here → failed_checks, never completed.
        assert done["status"] == "failed_checks"
        events = json.dumps(bus.on(f"/runs/{started['run_id']}/events"))
        assert "k-123456" not in events and "secret is ***" in events     # redacted on the bus
    finally:
        await svc.on_stop()


@pytest.mark.asyncio
async def test_cloud_agent_refused_without_cloud_ok(bus, tmp_path):
    svc = await _service(tmp_path)
    try:
        started = await svc.m_start_run("simple", agent="claude_code")
        done = await svc.m_wait_run(started["run_id"], timeout_seconds=10)
        assert done["status"] == "error" and "local_only" in (done["error"] or "")
    finally:
        await svc.on_stop()


@pytest.mark.asyncio
async def test_halt_cancels_runs_and_blocks_new_ones(bus, tmp_path, monkeypatch):
    SEEN["slow"] = True
    svc = await _service(tmp_path)
    try:
        started = await svc.m_start_run("simple")
        await asyncio.sleep(0.3)
        await svc.on_halt({"level": "stop", "source": "test"})
        done = await svc.m_wait_run(started["run_id"], timeout_seconds=10)
        assert done["status"] == "cancelled"
        from botgear.runtime import halt
        monkeypatch.setattr(halt, "is_halted", lambda: True)
        assert "HALTED" in (await svc.m_start_run("simple"))["error"]
    finally:
        await svc.on_stop()


@pytest.mark.asyncio
async def test_concurrency_limit_and_unknown_workflow(bus, tmp_path):
    SEEN["slow"] = True
    svc = await _service(tmp_path, max_concurrent_runs=1)
    try:
        first = await svc.m_start_run("simple")
        assert "already running" in (await svc.m_start_run("simple"))["error"]
        assert "no runnable workflow" in (await svc.m_start_run("nope"))["error"]
        await svc.m_cancel_run(first["run_id"])
        assert (await svc.m_wait_run(first["run_id"], timeout_seconds=10))["status"] == "cancelled"
    finally:
        await svc.on_stop()


@pytest.mark.asyncio
async def test_state_shows_live_runs(bus, tmp_path):
    SEEN["slow"] = True
    svc = await _service(tmp_path)
    try:
        started = await svc.m_start_run("simple")
        await asyncio.sleep(0.5)
        state = bus.on("/agent/agent/state")[-1]
        live = state["active_runs"][0]
        assert live["run_id"] == started["run_id"] and live["tool_calls"] >= 1
        assert state["residency"] == "local_only" and state["mcp_key_set"] is False
        await svc.m_cancel_run(started["run_id"])
        await svc.m_wait_run(started["run_id"], timeout_seconds=10)
    finally:
        await svc.on_stop()
