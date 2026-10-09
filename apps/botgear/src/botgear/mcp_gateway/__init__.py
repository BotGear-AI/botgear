# unmanaged
"""botgear's MCP gateway — external AI agents drive botgear's live
services over MCP, under one tool policy and HALT. See ``gateway.py`` and
docs/AGENT_ROADMAP.md (Phase 1).

Wiring (event_handlers): ``create_gateway()`` once, ``start()`` from the
backend's startup (inside its event loop), ``mount(app)`` from
``register_admin_routes``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

MCP_PATH = "/v1/mcp"
_gateway: Optional[Any] = None
_task: Optional[asyncio.Task] = None


_deps: Optional[Any] = None


def _auth_deps():
    global _deps
    if _deps is None:
        from auth import create_auth_dependencies
        from config import create_app_settings
        from botgear.models.config import Config
        _settings, provider = create_app_settings("botgear", Config)
        _deps = create_auth_dependencies(provider)
    return _deps


def _authenticate(headers: Dict[str, str]) -> Dict[str, Any]:
    """The REST API's own auth, from raw headers: X-API-Key (API client) or
    Authorization: Bearer <login token>. Raises HTTPException(401) when
    neither is valid."""
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    deps = _auth_deps()
    authz = headers.get("authorization")
    credentials = None
    if authz and authz.lower().startswith("bearer "):
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=authz[7:].strip())
    if not (headers.get("x-api-key") or credentials):
        raise HTTPException(status_code=401, detail="send X-API-Key (an API client) or a Bearer login token")
    return deps.get_current_user_or_service(
        credentials=credentials, x_api_key=headers.get("x-api-key"), authorization=authz, request=None,
    )


def create_gateway():
    """The process-wide gateway (created once)."""
    global _gateway
    if _gateway is None:
        from botgear.mcp_gateway.gateway import McpGateway
        from botgear.runtime.bus import get_bus
        from botgear.runtime.lifecycle import _resolve_data_dir
        try:
            from botgear.server import load_version
            version = (load_version() or {}).get("version", "")
        except Exception:  # noqa: BLE001
            version = ""
        _gateway = McpGateway(bus_getter=get_bus, data_dir=_resolve_data_dir(),
                              authenticate=_authenticate, version=str(version or ""))
    return _gateway


def start() -> None:
    """Start the gateway's background work. Must run inside the backend's
    event loop (event_handlers.on_startup does)."""
    global _task
    if _task is not None and not _task.done():
        return
    gateway = create_gateway()
    _task = asyncio.get_running_loop().create_task(gateway.run(), name="mcp-gateway")


def mount(app: Any) -> None:
    """Register ``/v1/mcp`` on the FastAPI app, ahead of any catch-all
    (e.g. the SPA fallback) so it can't be shadowed."""
    from starlette.routing import Route
    route = Route(MCP_PATH, endpoint=create_gateway(), methods=["GET", "POST", "DELETE"])
    app.router.routes.insert(0, route)
    logger.info("mcp_gateway: mounted at %s", MCP_PATH)
