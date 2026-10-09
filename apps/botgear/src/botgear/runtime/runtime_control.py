# unmanaged
"""Read-only request/reply surface for the runtime singleton.

The runtime is not a framework ``Service`` (the backend process IS the
runtime), so until now it published data topics but no ``meta`` — nothing
on the bus could *ask* it anything, and brain workflows had no runtime
tools at all. This module gives it the same wire contract every service
has:

  ``/runtime/runtime/meta``     (retained) self-description + methods,
                                 same shape as Service._build_meta_payload,
                                 so brain's tool catalog picks it up
  ``/runtime/runtime/control``  ``{"action": <name>, "reply_to": <topic>}``
                                 → the result is published to reply_to

Every action here is READ-ONLY, on purpose. Default-allow brain workflows
(e.g. general_assistant) can call anything on the bus that isn't blocked,
and a block rule can't be carved back open per action — so a runtime
action that changes something (install, stop, shutdown, config) must NOT
be added to this topic. Put such actions on a separate topic and add that
topic to SHARED_EXCLUDES in botgear/agent_surface/excludes.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from botgear.runtime.bus import get_bus
from botgear.runtime.identity import get_runtime_id


logger = logging.getLogger(__name__)

PROXY_ID = "runtime"
_ROOT = f"/runtime/{PROXY_ID}"
CONTROL_TOPIC = f"{_ROOT}/control"
META_TOPIC = f"{_ROOT}/meta"

_NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}


def _retained(topic: str) -> Optional[Dict[str, Any]]:
    try:
        msg = get_bus().retained_get(topic)
    except Exception:  # noqa: BLE001
        return None
    payload = msg.payload if hasattr(msg, "payload") else msg
    return payload if isinstance(payload, dict) else None


# ─── actions ─────────────────────────────────────────────────────────────


def get_time() -> Dict[str, Any]:
    """Current date and time on this runtime's host: local time with
    weekday and timezone, plus UTC and a unix timestamp."""
    now = datetime.now().astimezone()
    offset = now.strftime("%z")
    return {
        "local": now.isoformat(timespec="seconds"),
        # Built by hand — %-d / %-I are glibc-only.
        "spoken": f"{now:%A %B} {now.day} {now.year}, {now.hour % 12 or 12}:{now:%M %p %Z}",
        "weekday": now.strftime("%A"),
        "timezone": now.tzname(),
        "utc_offset": f"{offset[:3]}:{offset[3:]}",
        "utc": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "unix": round(time.time(), 3),
    }


def get_info() -> Dict[str, Any]:
    """Who this runtime is: botgear version, runtime id, host, OS, Python
    version, process id, and when it started / how long it has been up."""
    info = _retained("/runtime/info") or {}
    version = str(info.get("version") or "0.0.0")
    started_at = info.get("started_at")
    uptime_s = None
    if isinstance(started_at, str):
        try:
            uptime_s = round(time.time() - datetime.fromisoformat(started_at).timestamp())
        except ValueError:
            pass
    return {
        "runtime_id": info.get("id") or get_runtime_id(),
        # 0.0.0 is the dev/unknown stamp (see runtime/updater.py).
        "botgear_version": version,
        "dev_build": version == "0.0.0",
        "started_at": started_at,
        "uptime_s": uptime_s,
        "pid": os.getpid(),
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "python": platform.python_version(),
    }


def get_system_state() -> Dict[str, Any]:
    """Latest host metrics: CPU load, memory, root-disk usage, and this
    process's resource use (the retained /runtime/runtime/state snapshot)."""
    return _retained(f"{_ROOT}/state") or {"error": "system state not published yet"}


def get_power() -> Dict[str, Any]:
    """Battery level and charging status, AC power, and suspend/resume
    state (the retained /runtime/runtime/power snapshot). battery is null
    on machines without one."""
    return _retained(f"{_ROOT}/power") or {"error": "power state not published yet"}


def list_services() -> Dict[str, Any]:
    """Every service running on this runtime: proxy id, type, version and
    transport (in_process / subprocess)."""
    index = _retained(f"{_ROOT}/services") or {}
    services = [
        {k: s.get(k) for k in ("proxy_id", "type", "version", "transport")}
        for s in index.get("services") or [] if isinstance(s, dict)
    ]
    return {"count": len(services), "services": services}


# Read-only by contract — see the module docstring before adding to this.
ACTIONS: Dict[str, Callable[[], Dict[str, Any]]] = {
    "get_time": get_time,
    "get_info": get_info,
    "get_system_state": get_system_state,
    "get_power": get_power,
    "list_services": list_services,
}


def build_meta() -> Dict[str, Any]:
    return {
        "proxy_id": PROXY_ID,
        "type": "runtime",
        "version": "1.0.0",
        "transport": "in_process",
        "runtime_id": get_runtime_id() or None,
        "pid": os.getpid(),
        "topics_root": _ROOT,
        "topics": {
            "state": f"{_ROOT}/state",
            "control": CONTROL_TOPIC,
            "meta": META_TOPIC,
            "power": f"{_ROOT}/power",
            "services": f"{_ROOT}/services",
        },
        "methods": [
            {"name": name, "doc": (fn.__doc__ or "").strip() or None,
             "publishes": [], "publish_return": None, "args_schema": _NO_ARGS}
            for name, fn in ACTIONS.items()
        ],
    }


def handle(payload: Any) -> None:
    """Dispatch one control frame; reply on ``reply_to`` when given."""
    if not isinstance(payload, dict):
        return
    action = payload.get("action")
    reply_to = payload.get("reply_to")
    fn = ACTIONS.get(action) if isinstance(action, str) else None
    if fn is None:
        logger.warning("runtime.control: ignored unknown action %r", action)
        result: Any = {"error": f"unknown action: {action}"}
    else:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            logger.exception("runtime.control: action %r raised", action)
            result = {"error": str(exc)}
    if isinstance(reply_to, str) and reply_to:
        get_bus().publish_sync(reply_to, result)


# ─── lifecycle ───────────────────────────────────────────────────────────

_task: Optional[asyncio.Task[None]] = None


async def _run() -> None:
    async for msg in get_bus().subscribe(CONTROL_TOPIC, "runtime.control"):
        handle(msg.payload if hasattr(msg, "payload") else msg)


def start() -> None:
    """Idempotent — publish meta and start the control loop once."""
    global _task
    if _task is not None and not _task.done():
        return
    get_bus().publish_sync(META_TOPIC, build_meta(), retained=True)
    _task = asyncio.create_task(_run(), name="runtime.control")
    logger.info("runtime.control: started (topic=%s, actions=%s)", CONTROL_TOPIC, sorted(ACTIONS))


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _task = None
