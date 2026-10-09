# unmanaged
"""Subprocess discovery via /service_proxy/+/hello.

Subprocess services publish a retained hello message on every successful
(re)connect to the backend's bus. The runtime subscribes to the wildcard
and reconciles incoming hellos against the service_proxy DB row.

Three outcomes per hello:
  1. Row matches (proxy_id present, pid matches hello pid).
     → reaffirm running. No state change.
  2. Row exists but pid differs.
     → record from a previous backend lifetime OR a duplicate spawn.
     The hello pid is the live truth — adopt it, update the row,
     SIGTERM the orphan pid (best effort) IF it's still alive.
  3. Row missing or status=uninstalled.
     → a subprocess for a proxy we don't own. SIGTERM it.

The listener lives in a daemon thread so it can call into the (sync)
bus.subscribe + (sync) DB without contaminating the FastAPI event loop.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from database.factory import get_database_client
from botgear.runtime.bus import get_bus
from botgear.runtime import manifest_cache, proc_compat, process_manager


logger = logging.getLogger(__name__)

_HELLO_PATTERN = "/service_proxy/+/hello"
_CONFIG_PATCH_PATTERN = "/service_proxy/+/config_patch"
_METHODS_PATTERN = "/service_proxy/+/methods"
_SUBSCRIBER_ID = "runtime-discovery"

_thread: Optional[threading.Thread] = None
_started = threading.Event()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _proxy_id_from_topic(topic: str) -> Optional[str]:
    parts = topic.split("/")
    # ['', 'service_proxy', '<id>', 'hello']
    if len(parts) != 4 or parts[1] != "service_proxy" or parts[3] != "hello":
        return None
    return parts[2] or None


def _handle_hello(topic: str, payload: Any) -> None:
    proxy_id = _proxy_id_from_topic(topic)
    if not proxy_id or not isinstance(payload, dict):
        return
    hello_pid = payload.get("pid")
    if not isinstance(hello_pid, int) or hello_pid <= 0:
        return

    db = get_database_client()
    if db is None:
        return
    row = db.get_item("service_proxy", proxy_id)

    # Case 3: subprocess for a proxy we don't own.
    if not row or row.get("status") == "uninstalled":
        logger.warning(
            "discovery: orphan subprocess hello — pid=%s claims proxy=%s but no row exists; SIGTERM",
            hello_pid, proxy_id,
        )
        _kill(hello_pid)
        return

    row_pid = row.get("pid")
    if row_pid == hello_pid:
        # Case 1: same instance the row knows about. If status got
        # stamped to error/stopped while the process is actually
        # running, reaffirm it.
        if row.get("status") not in {"running", "starting"}:
            logger.info("discovery: %s reaffirmed running (pid=%s)", proxy_id, hello_pid)
            row["status"] = "running"
            row["error"] = None
            row["pid"] = hello_pid
            row["host"] = row.get("host") or "127.0.0.1"
            row["started_at"] = row.get("started_at") or _now_iso()
            db.update_item("service_proxy", proxy_id, row, include_nulls=True)
        return

    # Case 2: row has a different pid. Adopt the hello pid; SIGTERM the
    # orphan IF the recorded pid is actually alive AND different.
    if row_pid and row_pid != hello_pid and process_manager.pid_alive(row_pid):
        logger.warning(
            "discovery: %s — adopting pid=%s, killing orphan pid=%s",
            proxy_id, hello_pid, row_pid,
        )
        _kill(row_pid)
    else:
        logger.info(
            "discovery: %s — adopting pid=%s (row had pid=%s status=%s)",
            proxy_id, hello_pid, row_pid, row.get("status"),
        )

    row["pid"] = hello_pid
    row["status"] = "running"
    row["error"] = None
    row["started_at"] = row.get("started_at") or _now_iso()
    row["stopped_at"] = None
    db.update_item("service_proxy", proxy_id, row, include_nulls=True)


def _kill(pid: int) -> None:
    """Best-effort GRACEFUL stop of a subprocess (SIGTERM to its process
    group on POSIX / CTRL_BREAK on Windows) so its ``on_stop`` runs — e.g.
    an orphan arduino quiets Firmata + closes the serial port before exit.
    Fire-and-forget; the reconciler owns SIGKILL escalation."""
    proc_compat.request_graceful_stop(pid)


def _handle_config_patch(topic: str, payload: Any) -> None:
    """Merge a subprocess-published config patch into service_proxy.service_config
    and re-broadcast the new config_state so any other listeners see truth."""
    proxy_id = _proxy_id_from_topic_segment(topic, expected_tail="config_patch")
    if not proxy_id:
        return
    if not isinstance(payload, dict) or not payload:
        return
    db = get_database_client()
    if db is None:
        return
    row = db.get_item("service_proxy", proxy_id)
    if not row:
        logger.warning("discovery: config_patch for missing proxy=%s", proxy_id)
        return
    # stone 4b: config lives in the yml, not the row. Merge the subprocess's
    # patch onto its current config (yml/live) and persist to yml directly.
    from botgear.runtime import lifecycle
    current = lifecycle.proxy_config(proxy_id)
    merged = {**current, **payload}
    lifecycle.save_proxy_config(proxy_id, merged)
    # Re-broadcast retained so any other consumer (UI panels, future
    # services) sees the new truth without polling. ``merged`` is the
    # DECRYPTED config — mask any SecretStr field before it goes out on
    # the bus, since any WS-connected browser can subscribe to this
    # retained topic. See discovery._mask_secret_fields.
    get_bus().publish_sync(
        f"/service_proxy/{proxy_id}/config_state",
        _mask_secret_fields(row.get("service_meta_id"), merged),
        retained=True,
    )
    logger.info("discovery: config_patch applied for %s: %s", proxy_id, list(payload.keys()))


def _mask_secret_fields(type_id: Optional[str], config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Redact any field whose Pydantic type is ``SecretStr`` on the
    type's ``config_class`` before a config dict leaves the backend on a
    retained bus topic (``config_state`` is readable by any WS-connected
    browser, unlike the encrypted-at-rest yml). A set-but-masked value
    reads as ``"••••••••"``; unset stays whatever falsy value it was.

    Falls back to a small naming-convention denylist (``token``,
    ``secret``, ``password``, ``webhook_url``, ``api_key`` substrings)
    when the config_class can't be resolved — e.g. a subprocess service
    whose module imports its third-party SDK eagerly (arduino/pymata4,
    for instance). This is a best-effort net, not a type-driven
    guarantee, for those cases — see docs/TODO_MESSAGING.md's "known
    limitations" note. discord/slack/google_chat keep their SDK imports
    lazy specifically so the type-driven path resolves for them.
    """
    if not isinstance(config_dict, dict) or not config_dict or not type_id:
        return config_dict
    from botgear.runtime.boot import resolve_config_class
    secret_keys: set = set()
    config_class = resolve_config_class(type_id)
    if config_class is not None:
        try:
            from pydantic import SecretStr
            import typing
            for name, field in config_class.model_fields.items():
                ann = field.annotation
                if ann is SecretStr or SecretStr in typing.get_args(ann):
                    secret_keys.add(name)
        except Exception:  # noqa: BLE001
            secret_keys = set()
    if not secret_keys:
        secret_keys = {
            k for k in config_dict
            if any(tok in k.lower() for tok in ("token", "secret", "password", "webhook_url", "api_key"))
        }
    if not secret_keys:
        return config_dict
    masked = dict(config_dict)
    for k in secret_keys:
        if masked.get(k):
            masked[k] = "••••••••"
    return masked


def _handle_methods_manifest(topic: str, payload: Any) -> None:
    """Cache a subprocess's retained methods-manifest so the topology
    API can render its publishes + methods. A ``None`` payload (published
    on graceful shutdown) evicts the entry."""
    proxy_id = _proxy_id_from_topic_segment(topic, expected_tail="methods")
    if not proxy_id:
        return
    if payload is None:
        manifest_cache.remove(proxy_id)
        return
    if not isinstance(payload, dict):
        return
    manifest_cache.put(proxy_id, {
        "type_name": payload.get("type_name"),
        "transport": payload.get("transport") or "subprocess",
        "class_publishes": list(payload.get("class_publishes") or []),
        "methods": list(payload.get("methods") or []),
    })


def _proxy_id_from_topic_segment(topic: str, *, expected_tail: str) -> Optional[str]:
    """Generic version of _proxy_id_from_topic. Handles ``/service_proxy/{id}/{tail}``."""
    parts = topic.split("/")
    if len(parts) != 4 or parts[1] != "service_proxy" or parts[3] != expected_tail:
        return None
    return parts[2] or None


async def _consume_loop() -> None:
    bus = get_bus()
    # Two independent subscriptions, each for a wildcard pattern. We
    # use asyncio.gather so a slow handler on one topic doesn't block
    # the other.
    async def _consume_hellos() -> None:
        async for msg in bus.subscribe(_HELLO_PATTERN, _SUBSCRIBER_ID + ":hello"):
            try:
                _handle_hello(msg.topic, msg.payload)
            except Exception:  # noqa: BLE001
                logger.exception("discovery: hello handler raised for topic=%s", msg.topic)

    async def _consume_config_patches() -> None:
        async for msg in bus.subscribe(_CONFIG_PATCH_PATTERN, _SUBSCRIBER_ID + ":config"):
            try:
                _handle_config_patch(msg.topic, msg.payload)
            except Exception:  # noqa: BLE001
                logger.exception("discovery: config_patch handler raised for topic=%s", msg.topic)

    async def _consume_methods() -> None:
        async for msg in bus.subscribe(_METHODS_PATTERN, _SUBSCRIBER_ID + ":methods"):
            try:
                _handle_methods_manifest(msg.topic, msg.payload)
            except Exception:  # noqa: BLE001
                logger.exception("discovery: methods handler raised for topic=%s", msg.topic)

    await asyncio.gather(_consume_hellos(), _consume_config_patches(), _consume_methods())


def _thread_main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _started.set()
    try:
        loop.run_until_complete(_consume_loop())
    except Exception:  # noqa: BLE001
        logger.exception("discovery: consume loop crashed")
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:  # noqa: BLE001
            pass
        loop.close()


def start() -> None:
    """Spin up the discovery listener. Idempotent."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_thread_main, name="rlx-discovery", daemon=True)
    _thread.start()
    _started.wait(timeout=2.0)
    logger.info("discovery: listening on %s, %s and %s",
                _HELLO_PATTERN, _CONFIG_PATCH_PATTERN, _METHODS_PATTERN)
