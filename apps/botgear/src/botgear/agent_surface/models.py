# unmanaged
"""Data shapes of botgear's agent surface: what a tool is, what calling it
returned, which tools a caller may use, and the verdict on one call.

Every mind that drives botgear (brain's engine today; the MCP gateway and
agent_harness runs next — see docs/AGENT_ROADMAP.md) speaks these, so a
policy or audit record means the same thing whichever agent produced it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ToolDescriptor(BaseModel):
    """One callable service action, as advertised to an agent. Built from
    the service's retained ``/{type}/{id}/meta`` (``@service_method`` name,
    docstring, signature-derived ``args_schema``) plus the control topic
    the action is dispatched to."""
    topic: str = Field(..., description="e.g. /video/video-1/control")
    action: str = Field(..., description="e.g. capture_frame")
    description: str = ""
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema for the action's arguments.",
    )

    @property
    def name(self) -> str:
        """Catalog key + provider-neutral tool id: ``<topic>::<action>``."""
        return f"{self.topic}::{self.action}"


class ToolResult(BaseModel):
    """What came back from one tool call."""
    status: Literal["ok", "error", "timeout"] = "ok"
    value: Any = None
    error: Optional[str] = None
    duration_ms: int = 0


class ToolPattern(BaseModel):
    """One allow/block rule: a topic glob + the actions it covers."""
    topic: str = Field(..., description="Topic glob, e.g. /video/*/control or /movement/* ")
    actions: List[str] = Field(
        default_factory=list,
        description="Action names allowed on the matched topic. Empty/omitted = no actions.",
    )


class ToolPolicy(BaseModel):
    """Which tools a caller may use. Evaluation (``policy.check_call``):
    ``blocked`` ALWAYS wins — no ``allowed`` entry can carve an exception
    back into a block; then ``allowed``; then ``default_policy``. A policy
    loaded with ``default_policy: allow`` always carries the shared
    hardware excludes (``excludes.SHARED_EXCLUDES``) in ``blocked``, so a
    new dangerous capability can't become reachable-by-default just
    because one caller's rules forgot it."""
    allowed: List[ToolPattern] = Field(default_factory=list)
    blocked: List[ToolPattern] = Field(default_factory=list)
    default_policy: Literal["deny", "allow"] = Field(
        "deny",
        description=(
            "What happens when a proposed (topic, action) matches neither "
            "`allowed` nor `blocked`. 'deny' (default): nothing is "
            "reachable unless explicitly allowed. 'allow': anything not "
            "blocked is callable — and the shared hard-exclude list comes "
            "with it, unconditionally."
        ),
    )


class SafetyVerdict(BaseModel):
    allowed: bool
    reason: str = ""
    # Which guard said no. The surface sets allow_list / block_list /
    # unknown_target / halted; brain's engine adds its own run budgets.
    guard: Optional[Literal[
        "allow_list", "block_list", "max_steps", "max_tokens", "timeout",
        "approval", "unknown_target", "schema", "halted",
    ]] = None


class ToolCallRecord(BaseModel):
    """Audit record for one tool call — one line of a run's
    ``tool_calls.jsonl`` and the payload streamed to UIs."""
    ts: str
    step: int
    tool_call_id: str
    topic: str
    action: str
    args: Dict[str, Any]
    result: ToolResult
