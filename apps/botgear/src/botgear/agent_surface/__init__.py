# unmanaged
"""botgear's agent surface: what any AI agent ("mind") needs to drive
botgear — discover the live tools, be held to one tool policy (incl. the
shared hardware excludes and HALT), call actions over the bus, and record
each call.

Extracted from the brain service (Phase 0 of docs/AGENT_ROADMAP.md) so
brain, the planned MCP gateway and agent_harness runs share one
implementation and one policy instead of each growing its own.

    from botgear.agent_surface import ToolExecutor, check_call, load_policy_file

Modules: ``models`` (shapes), ``catalog`` (live tool catalog), ``policy``
(allow/block/default + HALT), ``excludes`` (the shared hard excludes),
``invoke`` (bus request/reply).
"""
from botgear.agent_surface.catalog import (
    Catalog,
    add_meta,
    control_topic_of,
    descriptors_from_meta,
    evict_proxy,
    released_proxy,
    watch_meta,
    watch_releases,
)
from botgear.agent_surface.excludes import SHARED_EXCLUDES, shared_excludes
from botgear.agent_surface.invoke import ToolExecutor, bus_reply_iter
from botgear.agent_surface.models import (
    SafetyVerdict,
    ToolCallRecord,
    ToolDescriptor,
    ToolPattern,
    ToolPolicy,
    ToolResult,
)
from botgear.agent_surface.policy import (
    action_matches,
    capability_prefix,
    check_call,
    halt_verdict,
    load_policy_file,
    matches_any,
    policy_from_dict,
    topic_matches,
)

__all__ = [
    "Catalog", "add_meta", "control_topic_of", "descriptors_from_meta", "evict_proxy",
    "released_proxy", "watch_meta", "watch_releases",
    "SHARED_EXCLUDES", "shared_excludes",
    "ToolExecutor", "bus_reply_iter",
    "SafetyVerdict", "ToolCallRecord", "ToolDescriptor", "ToolPattern", "ToolPolicy", "ToolResult",
    "action_matches", "capability_prefix", "check_call", "halt_verdict", "load_policy_file",
    "matches_any", "policy_from_dict", "topic_matches",
]
