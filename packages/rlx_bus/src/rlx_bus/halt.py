"""Runtime-wide HALT — the one stop signal every service obeys.

A halt is a broadcast COMMAND, not an RPC: anyone (the UI's Stop button
via ``POST /v1/system/halt``, a keyboard hotkey binding, an STT service
that heard a stop word, a brain loop breaker) publishes a payload on
``HALT_TOPIC`` and every running service reacts on its own, in parallel,
with no relay hop and no LLM in the path:

    {"level": "stop" | "emergency", "source": str, "reason": str, "id": str, "ts": float}

``stop`` silences + idles conversation (speech, speaker, STT queues,
brain runs); ``emergency`` additionally means "stop moving" for services
that move things. ``RESUME_TOPIC`` lifts the brain's start latch (the
only service that refuses work while halted). The runtime keeps the
current status retained on ``HALT_STATE_TOPIC`` for UIs.

Services opt in by overriding ``on_halt(event)`` / ``on_resume(event)``
(default no-ops) — the framework subscribes for them, in-process and
subprocess alike.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict

HALT_TOPIC = "/runtime/halt"
RESUME_TOPIC = "/runtime/resume"
HALT_STATE_TOPIC = "/runtime/halt_state"

HALT_LEVELS = ("stop", "emergency")


def halt_event(level: str = "stop", source: str = "", reason: str = "") -> Dict[str, Any]:
    """Build a well-formed halt/resume payload (``level`` coerced to a
    known value so a typo can never weaken a stop below ``stop``)."""
    return {
        "level": level if level in HALT_LEVELS else "stop",
        "source": source or "unknown",
        "reason": reason or "",
        "id": uuid.uuid4().hex,
        "ts": time.time(),
    }
