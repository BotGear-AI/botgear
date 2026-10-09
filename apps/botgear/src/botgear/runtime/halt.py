# unmanaged
"""Runtime side of the HALT broadcast (see ``rlx_bus.halt``).

Services react to ``/runtime/halt`` on their own — this module is NOT a
relay in the stop path. It does two things:

  * ``request_halt`` / ``request_resume`` — publish the broadcast for
    callers inside the backend (``POST /v1/system/halt``, the brain's loop
    breaker).
  * Track the current status for everyone else: a listener thread watches
    BOTH topics (halts also arrive from a keyboard hotkey or an STT stop
    word) and keeps ``/runtime/halt_state`` retained, so the UI banner and
    the brain's start latch (``is_halted``) see one consistent answer.

The halted state is in-memory: a backend restart comes back un-halted (a
restart already stops every service, which is the stronger stop).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from rlx_bus.halt import HALT_STATE_TOPIC, HALT_TOPIC, RESUME_TOPIC, halt_event

from botgear.runtime.bus import get_bus

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_seen: deque = deque(maxlen=256)          # event ids already applied (direct call + echo via bus)
_state: Dict[str, Any] = {
    "halted": False,
    "level": None,
    "source": None,
    "reason": None,
    "since": None,
    "count": 0,                           # halts since boot
}
_thread: Optional[threading.Thread] = None
_started = threading.Event()


def state() -> Dict[str, Any]:
    with _lock:
        return dict(_state)


def is_halted() -> bool:
    with _lock:
        return bool(_state["halted"])


def request_halt(level: str = "stop", source: str = "api", reason: str = "") -> Dict[str, Any]:
    """Broadcast a halt. Returns the resulting state."""
    ev = halt_event(level, source, reason)
    _apply("halt", ev)
    get_bus().publish_sync(HALT_TOPIC, ev)
    logger.warning("HALT (%s) from %s: %s", ev["level"], ev["source"], ev["reason"])
    return state()


def request_resume(source: str = "api", reason: str = "") -> Dict[str, Any]:
    """Lift a halt. Returns the resulting state."""
    ev = halt_event("stop", source, reason)
    _apply("resume", ev)
    get_bus().publish_sync(RESUME_TOPIC, ev)
    logger.warning("RESUME from %s: %s", ev["source"], ev["reason"])
    return state()


def _apply(kind: str, ev: Any) -> None:
    if not isinstance(ev, dict):
        ev = {}
    ev_id = ev.get("id")
    with _lock:
        if ev_id and ev_id in _seen:
            return
        if ev_id:
            _seen.append(ev_id)
        if kind == "halt":
            # An emergency upgrades a running stop; a later plain stop
            # never downgrades an emergency.
            level = "emergency" if "emergency" in (ev.get("level"), _state["level"]) else "stop"
            if not _state["halted"]:
                _state["since"] = datetime.now(timezone.utc).isoformat()
            _state.update(halted=True, level=level,
                          source=ev.get("source"), reason=ev.get("reason"))
            _state["count"] += 1
        else:
            _state.update(halted=False, level=None, source=ev.get("source"),
                          reason=ev.get("reason"), since=None)
        snapshot = dict(_state)
    try:
        get_bus().publish_sync(HALT_STATE_TOPIC, snapshot, retained=True)
    except Exception:  # noqa: BLE001
        logger.exception("halt: state publish failed")


async def _consume() -> None:
    bus = get_bus()

    async def _watch(topic: str, kind: str) -> None:
        async for msg in bus.subscribe(topic, f"runtime-halt-state:{kind}"):
            try:
                _apply(kind, msg.payload)
            except Exception:  # noqa: BLE001
                logger.exception("halt: applying %s failed", kind)

    await asyncio.gather(_watch(HALT_TOPIC, "halt"), _watch(RESUME_TOPIC, "resume"))


def _thread_main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _started.set()
    try:
        loop.run_until_complete(_consume())
    except Exception:  # noqa: BLE001
        logger.exception("halt: listener crashed")
    finally:
        loop.close()


def start() -> None:
    """Start the state listener + publish the initial (un-halted) state.
    Idempotent."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_thread_main, name="rlx-halt", daemon=True)
    _thread.start()
    _started.wait(timeout=2.0)
    get_bus().publish_sync(HALT_STATE_TOPIC, state(), retained=True)
