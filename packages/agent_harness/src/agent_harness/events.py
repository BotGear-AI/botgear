"""Normalized agent event stream.

Every driver translates its vendor's native events into AgentEvent. Only the
types below are normalized; the vendor's original event is always kept in
`raw` as an escape hatch, so nothing is lost by not normalizing it.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

EVENT_TYPES = frozenset({
    "message",             # assistant text
    "status",              # lifecycle / progress (init, model, session id, ...)
    "tool_call",           # agent invoked a tool
    "tool_result",         # a tool returned
    "file_change",         # agent wrote/edited a file
    "command",             # agent ran a shell command
    "permission_request",  # agent asked for (or was denied) a permission
    "usage",               # tokens / cost
    "error",
    "completed",           # terminal event; data carries the final message
})


@dataclass
class AgentEvent:
    type: str
    agent: str
    data: dict[str, Any] = field(default_factory=dict)
    raw: Any = None
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {self.type!r}")

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)
