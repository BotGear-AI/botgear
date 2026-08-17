# unmanaged
"""Subprocess service authentication.

Subprocess services that need to talk to the backend's bus (publish state,
subscribe to their control topic) authenticate via a long-lived JWT minted
at backend boot. The token is signed with the same JWT_SECRET_KEY as user
tokens, so the existing /v1/ws decode path accepts it without modification.

The token is generated lazily on first request and cached in module state
for the lifetime of the backend process. We do NOT persist it to disk:
JWT_SECRET_KEY might rotate, and a fresh boot mints a fresh token cheaply.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import jwt


logger = logging.getLogger(__name__)

_CACHED_TOKEN: Optional[str] = None


def _jwt_secret() -> str:
    # Delegate to the single source (auth.jwt_util) so the env var + dev fallback
    # stay normalized instead of re-typed here.
    from auth import get_jwt_secret
    return get_jwt_secret()


def get_subprocess_token() -> str:
    """Long-lived JWT for subprocess bus access.

    Payload mirrors the user-token shape the WS endpoint expects: ``sub`` +
    ``user.roles=['Admin']`` so the connection isn't blocked by the
    require-role check. There's no ``exp`` claim — the token outlives the
    subprocess but is invalidated when JWT_SECRET_KEY rotates.
    """
    global _CACHED_TOKEN
    if _CACHED_TOKEN is not None:
        return _CACHED_TOKEN
    payload = {
        "sub": "subprocess",
        "user": {
            "id": "subprocess",
            "email": "subprocess@botgear",
            "roles": ["Admin"],
            "status": "active",
        },
    }
    _CACHED_TOKEN = jwt.encode(payload, _jwt_secret(), algorithm="HS256")
    return _CACHED_TOKEN


def backend_url() -> str:
    """Best-effort 'where to reach the backend' URL for subprocess clients.

    Reads settings.port; defaults to localhost. Subprocess services run on
    the same host today, so localhost is correct. When we add remote
    workers, this becomes the bus broker address.

    The scheme MUST track ``settings.ssl_enabled``: when the backend serves
    TLS (self-signed), a subprocess that connects with plain ``ws://`` reads
    the TLS handshake as a bad HTTP response ("line without CRLF") and loops
    forever in reconnect, so it never subscribes to its control topic and
    silently stops receiving actions. ``_to_ws_url`` maps https→wss, so
    returning ``https://`` here is what makes the subprocess speak ``wss://``.
    """
    try:
        from config import get_settings
        s = get_settings()
        port = getattr(s, "port", None) or 8001
        scheme = "https" if getattr(s, "ssl_enabled", False) else "http"
    except Exception:  # noqa: BLE001
        port = 8001
        scheme = "http"
    return f"{scheme}://localhost:{port}"
