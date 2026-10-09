# unmanaged
"""Topic-pattern based allow/deny enforcement for proposed tool calls.

The bus is the ultimate enforcer — a publish either matches a
subscription pattern or doesn't. The safety gate sits in front of the
bus publish for tool calls so a proposed action is checked against
the workflow's allow/deny rules BEFORE going on the wire. Same
matching grammar as bus topic subscriptions.

Pattern grammar:
  *           one path segment ("any instance":  /video/*/control matches /video/video-1/control)
  @**         opt-in to federation suffixes      (/arduino/*@**/control matches /arduino/arduino-1@funny-droid/control)
  literal     exact match

Evaluation:
  1. Any blocked match  → DENY (blocked ALWAYS wins — unconditional,
                          regardless of default_policy below; there is
                          no way for an allowed-rule to carve an
                          exception back into a block)
  2. Any allowed match  → ALLOW
  3. Otherwise          → workflow.allowed_tools.default_policy decides:
                          "deny" (default — unchanged for every workflow
                          that doesn't opt in) or "allow" (inverted
                          default for that one workflow — see
                          ToolPolicy.default_policy and
                          botgear/agent_surface/excludes.py for the
                          mandatory hard-exclude list that comes with it)
"""
from __future__ import annotations

import logging
from typing import Optional

# The matching grammar + policy evaluation are botgear's shared agent
# surface (docs/AGENT_ROADMAP.md, Phase 0) — one policy for every mind.
# Re-exported under brain's historical names.
from botgear.agent_surface.policy import (  # noqa: F401
    action_matches as _action_matches,
    capability_prefix,
    check_call,
    matches_any as _matches_any,
    topic_matches as _topic_matches,
)
from brain.schemas import ProposedAction, SafetyVerdict, ToolDescriptor, Workflow


logger = logging.getLogger(__name__)


def check_tool_call(
    workflow: Workflow,
    action: ProposedAction,
    *,
    tool_catalog: Optional[dict[str, ToolDescriptor]] = None,
) -> SafetyVerdict:
    """Validate one proposed action against the workflow's tool policy
    + the live tool catalog. Returns a SafetyVerdict — never raises."""
    if action.kind != "tool":
        return SafetyVerdict(allowed=True, reason="terminal action")
    return check_call(
        workflow.allowed_tools, action.topic or "", action.action or "", catalog=tool_catalog,
    )


def check_max_steps(workflow: Workflow, steps_used: int) -> SafetyVerdict:
    if steps_used >= workflow.max_steps:
        return SafetyVerdict(
            allowed=False,
            guard="max_steps",
            reason=f"workflow.max_steps={workflow.max_steps} exhausted (used {steps_used})",
        )
    return SafetyVerdict(allowed=True)


def check_max_tokens(workflow: Workflow, tokens_used: int) -> SafetyVerdict:
    limit = workflow.max_tokens_per_run
    if limit is not None and tokens_used > limit:
        return SafetyVerdict(
            allowed=False,
            guard="max_tokens",
            reason=f"workflow.max_tokens_per_run={limit} exceeded (used {tokens_used})",
        )
    return SafetyVerdict(allowed=True)
