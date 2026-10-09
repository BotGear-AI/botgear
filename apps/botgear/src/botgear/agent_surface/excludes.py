# unmanaged
"""Hard excludes every ``default_policy: allow`` tool policy inherits.

This is the ONE place to update when a new category of service must be
off-limits to agents by default. It isn't optional and isn't referenced
per caller by hand — deliberately, so a new dangerous capability can't
slip through because one workflow's (or one MCP client's) rules forgot
to copy a block rule. (Was ``repo/brain/1.0.0/workflows/_shared/
excluded_tools.yaml`` until the agent surface moved out of brain; now a
constant so it ships in every packaging, frozen builds included.)

Categories:

* movement / arm / system / video — the same categories every
  default-deny brain workflow already blocks defensively (e.g.
  conversation_session, conversation_servo, mic_switch_control). Under
  default-deny those blocks are belt-and-suspenders; under default-allow
  they're load-bearing, so they live here instead of being copy-pasted.
* sabertooth / motor_control — the physical drive-motor surface
  (Sabertooth 2x25 controller + the generic motor_controller consumer in
  front of it). Named explicitly: a default-allow caller could otherwise
  drive real motors the moment it exists — a physical hazard, not a
  scope question.

Topic-glob limitation: a ToolDescriptor carries topic + action, no
capability tag, so this can only exclude known topic-prefix families, not
"anything implementing motor_controller". A new motor driver with a
different topic prefix needs its own line here.

``/runtime/runtime/control`` is deliberately NOT listed: it is read-only
by contract (see ``botgear/runtime/runtime_control.py``). If the runtime
grows actions that change something, put them on a separate topic and
block that topic here — blocks can't be opened per action, so mixing them
into /control would force blocking the read-only ones too.
"""
from __future__ import annotations

from typing import List

from botgear.agent_surface.models import ToolPattern

SHARED_EXCLUDES: List[ToolPattern] = [
    ToolPattern(topic="/movement/*/control", actions=["*"]),
    ToolPattern(topic="/arm/*/control", actions=["*"]),
    ToolPattern(topic="/system/*/control", actions=["*"]),
    ToolPattern(topic="/video/*/control", actions=["*"]),
    ToolPattern(topic="/sabertooth/*/control", actions=["*"]),
    ToolPattern(topic="/motor_control/*/control", actions=["*"]),
]


def shared_excludes() -> List[ToolPattern]:
    """A fresh copy (callers may extend the list they get)."""
    return [p.model_copy(deep=True) for p in SHARED_EXCLUDES]
