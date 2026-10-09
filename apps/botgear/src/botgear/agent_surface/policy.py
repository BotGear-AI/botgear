# unmanaged
"""Topic-pattern allow/deny enforcement for tool calls — the one policy
every mind is held to.

The bus is the ultimate enforcer (a publish either matches a subscription
or doesn't); this sits in front of the publish so a proposed call is
checked against the caller's rules BEFORE it goes on the wire. Same
matching grammar as bus topic subscriptions.

Pattern grammar:
  *           one path segment ("any instance": /video/*/control matches /video/video-1/control)
  @**         opt-in to federation suffixes (/arduino/*@**/control matches /arduino/arduino-1@funny-droid/control)
  literal     exact match

Evaluation (``check_call``):
  1. Any blocked match  → DENY. Blocked ALWAYS wins, whatever
                          default_policy says; an allowed rule can't
                          carve an exception back into a block.
  2. Any allowed match  → ALLOW (if a catalog is given, the action must
                          also be live).
  3. Otherwise          → ``policy.default_policy``: "deny" (default) or
                          "allow". A policy loaded with "allow" carries
                          the shared hard excludes (``excludes.py``).
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import yaml

from botgear.agent_surface.excludes import shared_excludes
from botgear.agent_surface.models import SafetyVerdict, ToolDescriptor, ToolPattern, ToolPolicy


def _strip_federation(s: str) -> tuple[str, bool]:
    """Strip an inline ``@<peer>`` suffix from any segment of a topic.

    Returns ``(unfederated, was_federated)``. The federation suffix lives
    WITHIN a path segment (``/video/video-1@funny-droid/control``), so we
    walk the segments — a top-level ``partition('@')`` would chop off the
    rest of the path too."""
    parts = s.split("/")
    federated = False
    for i, part in enumerate(parts):
        if "@" in part:
            seg_base, _, _peer = part.partition("@")
            parts[i] = seg_base
            federated = True
    return "/".join(parts), federated


def topic_matches(pattern: str, topic: str) -> bool:
    """Glob a topic against a pattern. ``*`` matches exactly one path
    segment (fnmatch's greedy ``*`` is the wrong shape for bus topics);
    ``@**`` in a pattern opts into federated topics, which otherwise never
    match."""
    pattern_base, _ = _strip_federation(pattern.replace("@**", ""))
    federation_opt_in = "@**" in pattern
    topic_base, topic_federated = _strip_federation(topic)

    if topic_federated and not federation_opt_in:
        return False

    p_parts = pattern_base.split("/")
    t_parts = topic_base.split("/")
    if len(p_parts) != len(t_parts):
        return False
    return all(fnmatch.fnmatchcase(tp, pp) for pp, tp in zip(p_parts, t_parts))


def capability_prefix(topic: str) -> str:
    """First path segment of a topic (``/speech/speech-1/control`` →
    ``"speech"``): the grouping key for lazy tool disclosure and
    ``describe_capability``. A practical proxy for "capability", not the
    formal ``implements:`` name (ToolDescriptor doesn't carry that)."""
    parts = topic.strip("/").split("/")
    return parts[0] if parts and parts[0] else topic


def action_matches(actions: list[str], proposed: str) -> bool:
    """``"*"`` = any action on the topic; an empty list matches nothing, so
    a rule has to name something explicit to allow anything."""
    return "*" in actions or proposed in actions


def matches_any(patterns: Iterable[ToolPattern], topic: str, action: str) -> Optional[ToolPattern]:
    for p in patterns:
        if topic_matches(p.topic, topic) and action_matches(p.actions, action):
            return p
    return None


def check_call(
    policy: ToolPolicy,
    topic: str,
    action: str,
    *,
    catalog: Optional[Dict[str, ToolDescriptor]] = None,
) -> SafetyVerdict:
    """Validate one (topic, action) against a policy and, optionally, the
    live catalog. Never raises."""
    blocked = matches_any(policy.blocked, topic, action)
    if blocked is not None:
        return SafetyVerdict(
            allowed=False,
            guard="block_list",
            reason=f"matched blocked pattern: topic={blocked.topic!r} action={action!r}",
        )

    allowed = matches_any(policy.allowed, topic, action)
    if allowed is None:
        if policy.default_policy == "allow":
            return SafetyVerdict(
                allowed=True,
                reason=(
                    f"default_policy=allow — topic={topic!r} action={action!r} "
                    f"matched no blocked rule; this workflow's inverted "
                    f"default permits it"
                ),
            )
        return SafetyVerdict(
            allowed=False,
            guard="allow_list",
            reason=(
                f"no allow rule matches topic={topic!r} action={action!r} "
                f"(default-deny — workflow.allowed_tools.allowed needs an entry)"
            ),
        )

    if catalog is not None and f"{topic}::{action}" not in catalog:
        return SafetyVerdict(
            allowed=False,
            guard="unknown_target",
            reason=f"{topic} has no live service exposing action {action!r}",
        )

    return SafetyVerdict(allowed=True, reason=f"matched allow rule {allowed.topic!r}")


def halt_verdict() -> Optional[SafetyVerdict]:
    """A denial while the runtime is HALTed (``/runtime/halt``), else None.
    For minds that don't get the HALT broadcast themselves (an external
    agent calling in through a gateway): every call must check this."""
    try:
        from botgear.runtime import halt
    except Exception:  # noqa: BLE001 — outside the backend (tests, tools)
        return None
    if not halt.is_halted():
        return None
    st = halt.state()
    return SafetyVerdict(
        allowed=False,
        guard="halted",
        reason=f"runtime is HALTED by {st.get('source') or 'unknown'}"
               f"{': ' + st['reason'] if st.get('reason') else ''} — resume first",
    )


def policy_from_dict(raw: Optional[Dict[str, Any]]) -> ToolPolicy:
    """Build a ToolPolicy from an ``allowed_tools.yaml``-shaped dict
    (``allowed`` / ``blocked`` / ``default_policy``). ``default_policy:
    allow`` merges in the shared hard excludes — mandatory, deduped on
    (topic, actions) so a caller that already wrote the same rule doesn't
    get a confusing duplicate."""
    raw = raw or {}
    default_policy = raw.get("default_policy", "deny")
    blocked = [ToolPattern(**p) for p in (raw.get("blocked") or [])]
    if default_policy == "allow":
        seen = {(p.topic, tuple(p.actions)) for p in blocked}
        for p in shared_excludes():
            key = (p.topic, tuple(p.actions))
            if key not in seen:
                blocked.append(p)
                seen.add(key)
    return ToolPolicy(
        allowed=[ToolPattern(**p) for p in (raw.get("allowed") or [])],
        blocked=blocked,
        default_policy=default_policy,
    )


def load_policy_file(path: Path) -> ToolPolicy:
    """Load an ``allowed_tools.yaml``. A missing file is default-deny with
    no rules: a caller without a policy can't call anything — a safe,
    obvious failure for a misconfiguration."""
    if not path.is_file():
        return ToolPolicy()
    return policy_from_dict(yaml.safe_load(path.read_text()) or {})
