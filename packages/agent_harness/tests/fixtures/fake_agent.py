"""A scripted fake agent for testing the harness without any vendor.

config["files"]: {workspace-relative path: content} to write.
config["fail"]: emit a fatal error instead of completing.
config["cloud"]: report cloud=True capabilities (for residency tests).
config["mcp"]: report mcp=True capabilities (for MCP-server tests).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from agent_harness.events import AgentEvent
from agent_harness.policy import Policy
from agent_harness.runtime import AgentTask, Capabilities


class FakeAgent:
    name = "fake"

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.capabilities = Capabilities(tools=True, streaming=True, resume=False, cloud=bool(config.get("cloud")),
                                         mcp=bool(config.get("mcp")))
        self.seen_task: AgentTask | None = None

    def run(self, workspace: Path, task: AgentTask, policy: Policy) -> Iterator[AgentEvent]:
        self.seen_task = task
        yield AgentEvent("status", self.name, {"phase": "init", "model": "fake-1", "session_id": "s-1"})
        if self.config.get("fail") and not self.config.get("files"):
            yield AgentEvent("error", self.name, {"message": "boom", "fatal": True})
            return
        if self.config.get("echo_env"):
            yield AgentEvent("message", self.name, {"text": "env: " + ",".join(f"{k}={v}" for k, v in sorted(task.env.items()))})
        for rel, content in self.config.get("files", {}).items():
            path = workspace / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            yield AgentEvent("file_change", self.name, {"path": rel})
        if self.config.get("fail"):   # wrote some files, then died (e.g. timeout)
            yield AgentEvent("error", self.name, {"message": "timed out", "fatal": True})
            return
        yield AgentEvent("usage", self.name, {"total_cost_usd": 0.0})
        yield AgentEvent("completed", self.name, {"message": "done"})

    def cancel(self) -> None:
        pass
