# unmanaged
"""Federation peers — service layer.

Backs the generated peer router (resource_slug "peers", methods list +
request). The "store" is the live peer_manager state, mirrored into the
runtime singleton's service_config.peers for restart-survival:

  GET  /v1/peers           → get_all_peer            (live peer snapshot)
  POST /v1/peers-request   → process_peer_request    (action: connect | disconnect | rescan)

Was the peer_api escape-hatch router; promoted to a normal DSL model
since a peer is a record (key/url/remote_id/state) with actions.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from database.factory import get_database_client
from fastapi import HTTPException, Request

from botgear.runtime import peer_manager

logger = logging.getLogger(__name__)


def _current_peer_urls() -> List[str]:
    return [info["url"] for info in peer_manager.peers().values()]


def _persist_peers(urls: List[str]) -> None:
    """Persist the active peers to the active set's runtime.yml so
    reboots reconnect with the SAME credential, not a silent downgrade
    to the shared-secret fallback (stone 4b: replaces the runtime
    row's service_config.peers). Each entry is ``url`` (string, Option
    A / shared-secret peer) or ``{url, secret}`` (Option B — see
    peer_manager.peer_secret). Best-effort."""
    try:
        from pathlib import Path
        from config import get_settings
        from botgear.runtime.config_sets import write_runtime_config_key
        data_dir = Path(get_settings().data_dir or "data")
        if not data_dir.is_absolute():
            data_dir = Path.cwd() / data_dir
        entries: List[Any] = []
        for url in sorted(set(urls)):
            secret = peer_manager.peer_secret(url)
            entries.append({"url": url, "secret": secret} if secret else url)
        write_runtime_config_key(data_dir, "peers", entries)
    except Exception:  # noqa: BLE001
        logger.exception("peer_service: failed to persist peers")


def _snapshot() -> List[Dict[str, Any]]:
    return [{"id": key, "key": key, **info} for key, info in sorted(peer_manager.peers().items())]


def get_all_peer(user: dict, request: Request) -> List[Dict[str, Any]]:
    """GET /v1/peers — snapshot of every known peer + state."""
    return _snapshot()


def _run_async(coro) -> None:
    """Run a peer_manager coroutine to completion. disconnect() must run on
    the manager's own event loop (where the connections live); we're called
    from a sync threadpool worker, so schedule it there and wait."""
    loop = getattr(peer_manager, "_manager_loop", None)
    if loop is not None:
        asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=15)
    else:
        asyncio.run(coro)


def process_peer_request(payload: Dict[str, Any], user: dict, request: Request) -> Dict[str, Any]:
    """POST /v1/peers-request {action: connect|disconnect|rescan|mint_peer_credential, …}.

    connect: {url, secret?} — open/look-up a connection (idempotent),
        persist it. ``secret`` is an api_client secret MINTED ON THE
        REMOTE side via that peer's own mint_peer_credential call (copy
        it over) — Option B, revocable per-peer. Omit it to use the
        shared JWT_SECRET_KEY subprocess token (Option A, the
        zero-config default auto-discovery still uses).
    disconnect: {key} — drop a peer by remote_id or url, unpersist.
    rescan: {} — rebuild the mDNS engine in place (see mdns.rescan's
        docstring for why this is needed: a laptop's Wi-Fi routinely
        isn't up yet when the runtime boots, so discovery silently
        never joins the real interface until something re-triggers it).
        Doesn't itself add peers to the snapshot — auto-connect (on by
        default) does that asynchronously as the browser rediscovers
        them; the client should re-poll GET /v1/peers a moment later.
    mint_peer_credential: {label} — create a new api_client row named
        "peer:<label>" with Admin role and a fresh secret, return it
        ONCE (same one-time-reveal convention as api_client create).
        This is the credential the OTHER side pairs with via
        connect's ``secret`` — mint on THIS runtime for "the peer I'm
        calling <label>", hand the returned secret to that peer's
        operator to use in ITS connect call back to this runtime. Does
        not itself connect anything.
    Returns {"metadata": {...}, "records": <refreshed peer snapshot>}."""
    action = (payload or {}).get("action")

    if action == "connect":
        url = (payload or {}).get("url")
        secret = (payload or {}).get("secret") or None
        if not url:
            raise HTTPException(400, "connect requires 'url'")
        try:
            pc = peer_manager.connect(url, secret=secret)
        except Exception as exc:  # noqa: BLE001
            logger.exception("peer_service.connect failed for %s", url)
            raise HTTPException(500, str(exc))
        _persist_peers(_current_peer_urls())
        meta = {"status": "success", "action": "connect",
                "key": pc.remote_id or pc.url, "url": pc.url,
                "remote_id": pc.remote_id, "state": pc.state.value,
                "auth": "api_key" if secret else "shared_secret"}

    elif action == "mint_peer_credential":
        label = (payload or {}).get("label")
        if not label or not isinstance(label, str):
            raise HTTPException(400, "mint_peer_credential requires 'label'")
        import secrets as _secrets
        import time as _time
        import uuid as _uuid
        db = get_database_client()
        if not db:
            raise HTTPException(500, "database unavailable")
        client_id = str(_uuid.uuid4())
        secret_value = _secrets.token_urlsafe(32)
        db.insert_item("api_client", client_id, {
            "id": client_id,
            "name": f"peer:{label}",
            "secret": secret_value,
            "roles": ["Admin"],
            "created": int(_time.time() * 1000),
        })
        logger.info("peer_service.mint_peer_credential: minted api_client %s for label=%r", client_id, label)
        meta = {"status": "success", "action": "mint_peer_credential",
                "id": client_id, "label": label, "secret": secret_value}

    elif action == "disconnect":
        key = (payload or {}).get("key") or (payload or {}).get("name")
        if not key:
            raise HTTPException(400, "disconnect requires 'key'")
        known = key in peer_manager.peers()
        _run_async(peer_manager.disconnect(key))
        if known:
            _persist_peers(_current_peer_urls())
        meta = {"status": "success", "action": "disconnect", "key": key, "disconnected": known}

    elif action == "rescan":
        from botgear.runtime import mdns
        restarted = mdns.rescan()
        meta = {"status": "success" if restarted else "noop", "action": "rescan", "restarted": restarted}

    else:
        raise HTTPException(400, f"unknown action {action!r} (expected connect|disconnect|rescan)")

    return {"metadata": meta, "records": _snapshot()}
