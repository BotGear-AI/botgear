# unmanaged
"""Mapping between botgear's tool catalog and MCP tools.

Every allowed service action becomes one MCP tool:

    name         <proxy>__<action>       e.g. switch-1__power_set
    description  the method docstring + which service / topic it acts on
    inputSchema  the signature-derived args_schema (rlx_bus/introspect.py)
    annotations  read-only actions (list_/get_/describe_/…) are marked
                 readOnlyHint, so clients can auto-approve them; everything
                 else keeps MCP's default (may change state). HINTS ONLY —
                 the gateway enforces policy itself; a client may ignore them.

Names stay within [A-Za-z0-9_-] and 50 characters, so a client prefix like
Claude Code's ``mcp__botgear__`` still fits the 64-character limit model
providers put on tool names.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, List, Tuple

import mcp_types as types

from botgear.agent_surface import ToolDescriptor, capability_prefix

MAX_NAME = 50
READ_ONLY_PREFIXES = (
    "list_", "get_", "describe_", "read_", "is_", "has_", "search_", "find_",
    "query_", "count_", "status", "ping",
)

BUILTIN_PREFIX = "botgear_"


def proxy_of(topic: str) -> str:
    parts = topic.strip("/").split("/")
    return parts[1] if len(parts) > 1 else parts[0]


def _clean(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_") or "x"


def tool_name(descriptor: ToolDescriptor) -> str:
    name = f"{_clean(proxy_of(descriptor.topic))}__{_clean(descriptor.action)}"
    if len(name) <= MAX_NAME:
        return name
    digest = hashlib.sha1(descriptor.name.encode()).hexdigest()[:8]
    return f"{name[:MAX_NAME - 9]}_{digest}"


def is_read_only(action: str) -> bool:
    return action.startswith(READ_ONLY_PREFIXES)


def annotations_for(descriptor: ToolDescriptor) -> types.ToolAnnotations:
    title = f"{descriptor.action} ({proxy_of(descriptor.topic)})"
    if is_read_only(descriptor.action):
        return types.ToolAnnotations(title=title, read_only_hint=True, destructive_hint=False,
                                     idempotent_hint=True)
    return types.ToolAnnotations(title=title)


def input_schema(descriptor: ToolDescriptor) -> Dict[str, Any]:
    schema = dict(descriptor.parameters or {})
    if schema.get("type") != "object":
        # MCP requires an object root; a service with no declared args takes none.
        schema = {"type": "object", "properties": schema.get("properties") or {}}
    schema.setdefault("properties", {})
    return schema


def to_mcp_tool(name: str, descriptor: ToolDescriptor) -> types.Tool:
    capability = capability_prefix(descriptor.topic)
    doc = descriptor.description.strip() or f"{descriptor.action} on {proxy_of(descriptor.topic)}."
    return types.Tool(
        name=name,
        description=f"{doc}\n\n[botgear service `{proxy_of(descriptor.topic)}` "
                    f"(capability `{capability}`), topic {descriptor.topic}]",
        input_schema=input_schema(descriptor),
        annotations=annotations_for(descriptor),
    )


def name_map(descriptors: Iterable[ToolDescriptor]) -> Dict[str, ToolDescriptor]:
    """MCP tool name → descriptor, collisions disambiguated with a digest."""
    out: Dict[str, ToolDescriptor] = {}
    for d in sorted(descriptors, key=lambda d: d.name):
        name = tool_name(d)
        if name in out and out[name].name != d.name:
            digest = hashlib.sha1(d.name.encode()).hexdigest()[:8]
            name = f"{name[:MAX_NAME - 9]}_{digest}"
        out[name] = d
    return out


# ─── built-in tools (not service actions) ──────────────────────────────


def builtin_tools() -> List[types.Tool]:
    return [
        types.Tool(
            name=f"{BUILTIN_PREFIX}list_capabilities",
            description="Overview of what this botgear runtime can do for you right now: each "
                        "capability (speech, stt, tasmota, …), its live instances and their actions, "
                        "plus whether the runtime is HALTed. Call this first to orient yourself.",
            input_schema={"type": "object", "properties": {}},
            annotations=types.ToolAnnotations(title="List capabilities", read_only_hint=True,
                                              destructive_hint=False, idempotent_hint=True),
        ),
        types.Tool(
            name=f"{BUILTIN_PREFIX}get_state",
            description="Read a service's current state (its retained state topic) without "
                        "calling it: e.g. service='switch-1' for the switch's relays, "
                        "service='mouth' to see whether it is speaking. Also service='runtime' "
                        "for the runtime's own state and 'halt' for the HALT status.",
            input_schema={"type": "object", "properties": {
                "service": {"type": "string", "description": "Service (proxy) id, e.g. 'switch-1'."},
            }, "required": ["service"]},
            annotations=types.ToolAnnotations(title="Get service state", read_only_hint=True,
                                              destructive_hint=False, idempotent_hint=True),
        ),
        types.Tool(
            name=f"{BUILTIN_PREFIX}halt",
            description="EMERGENCY STOP for the whole robot: immediately stops speech, playback "
                        "and every brain run, and refuses further actions until an operator "
                        "resumes. Use when something is going wrong or the operator asks you to "
                        "stop. level='emergency' also means 'stop moving'.",
            input_schema={"type": "object", "properties": {
                "reason": {"type": "string", "description": "Why you are stopping (shown to the operator)."},
                "level": {"type": "string", "enum": ["stop", "emergency"], "default": "stop"},
            }},
            annotations=types.ToolAnnotations(title="HALT the robot", read_only_hint=False,
                                              destructive_hint=False, idempotent_hint=True),
        ),
    ]


def capability_summary(allowed: Dict[str, ToolDescriptor]) -> Dict[str, Any]:
    """{capability: {instances: [...], actions: {proxy: [tool names]}}}."""
    caps: Dict[str, Dict[str, Any]] = {}
    for name, d in sorted(allowed.items()):
        cap = caps.setdefault(capability_prefix(d.topic), {"instances": [], "actions": {}})
        proxy = proxy_of(d.topic)
        if proxy not in cap["instances"]:
            cap["instances"].append(proxy)
        cap["actions"].setdefault(proxy, []).append(name)
    return caps


def state_topic_for(service: str, allowed: Dict[str, ToolDescriptor]) -> Tuple[str, str]:
    """(state topic, control topic) for a service id — taken from the live
    catalog so topic_remap is respected. Raises KeyError if not visible."""
    for d in allowed.values():
        if proxy_of(d.topic) == service:
            root = d.topic.rsplit("/", 1)[0]
            return f"{root}/state", d.topic
    raise KeyError(service)
