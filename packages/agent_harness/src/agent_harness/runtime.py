"""The agent runtime interface and driver registry.

Deliberately small (see the strategy doc): start a run in a prepared
workspace, stream normalized events, cancel. Provider-specific knobs travel
untranslated in the driver's `config` dict.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Protocol

from agent_harness.events import AgentEvent
from agent_harness.mcp_servers import McpServer
from agent_harness.policy import Policy


@dataclass(frozen=True)
class Capabilities:
    tools: bool       # can the agent read files / run scripts itself?
    streaming: bool
    resume: bool
    cloud: bool       # does data leave the host? (drives the residency gate)
    mcp: bool = False
    skills: bool = False  # discovers SKILL.md folders natively


@dataclass
class AgentTask:
    """Everything a driver may need; each driver uses what applies to it."""
    prompt: str                                   # rendered task.md
    outputs: list[str]                            # workspace-relative paths the agent must write
    system_context: str = ""                      # AGENTS.md text
    skills: dict[str, Path] = field(default_factory=dict)       # name -> skill dir inside the workspace
    read_dirs: list[Path] = field(default_factory=list)         # real (resolved) input dirs the agent may read
    context_commands: list[dict[str, str]] = field(default_factory=list)  # for tool-less runtimes
    python: str = "python3"                       # interpreter for skill scripts
    env: dict[str, str] = field(default_factory=dict)            # secrets for the agent's processes only
    self_checks: list[str] = field(default_factory=list)         # exact check commands the agent may run
    mcp_servers: list[McpServer] = field(default_factory=list)   # live tools (policy mcp=allow)


class AgentRuntime(Protocol):
    name: str
    capabilities: Capabilities

    def run(self, workspace: Path, task: AgentTask, policy: Policy) -> Iterator[AgentEvent]: ...

    def cancel(self) -> None: ...


_BUILTIN_DRIVERS = {
    "claude_code": "agent_harness.drivers.claude_code:ClaudeCodeDriver",
    "prompt_inject": "agent_harness.drivers.prompt_inject:PromptInjectDriver",
}
_REGISTRY: dict[str, Callable[[dict[str, Any]], AgentRuntime]] = {}


def register_driver(name: str, factory: Callable[[dict[str, Any]], AgentRuntime]) -> None:
    _REGISTRY[name] = factory


def available_drivers() -> list[str]:
    return sorted(set(_BUILTIN_DRIVERS) | set(_REGISTRY))


def get_driver(name: str, config: Optional[dict[str, Any]] = None) -> AgentRuntime:
    config = dict(config or {})
    if name in _REGISTRY:
        return _REGISTRY[name](config)
    if name in _BUILTIN_DRIVERS:
        module_name, attr = _BUILTIN_DRIVERS[name].split(":")
        # Lazy import: a driver's optional dependencies (e.g. openai) are only
        # needed when that driver is actually used.
        return getattr(importlib.import_module(module_name), attr)(config)
    raise ValueError(f"unknown agent {name!r}; available: {', '.join(available_drivers())}")
