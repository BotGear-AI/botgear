"""Generic per-request audit logging, generalized for any FastAPI app.

This is the default implementation server.py.j2's audit middleware falls back
to when an app opts in (`app.audit_enabled: true`) but does not define its own
`on_request_audit(request, status_code, latency_ms)` hook in event_handlers.py.
It knows nothing app-specific: no fixed log-type name an app's log pipeline
depends on, no domain fields (e.g. tenant_id semantics) beyond what
`request.state.audit_principal` (set by `get_current_user_or_service` in
auth_util.py) already carries generically.

An app with its own audit/telemetry conventions (its own log_type, its own
downstream index/pipeline, extra domain fields) should define its own hook
instead of relying on this default — see the docstring on the middleware in
server.py.j2.
"""
import logging
import time
from typing import Optional

from fastapi import Request

logger = logging.getLogger("auth.request_audit")

DEFAULT_AUDIT_LOG_TYPE = "api_request_audit"


def _client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path


def emit_default_audit_event(request: Request, status_code: int, latency_ms: float) -> None:
    """Emit one generic audit line for a completed request. Never raises."""
    try:
        principal = getattr(request.state, "audit_principal", None) or {}
        logger.info(
            "api_request_audit",
            extra={
                "log_type": DEFAULT_AUDIT_LOG_TYPE,
                "timestamp": int(time.time() * 1000),
                "method": request.method,
                "route": _route_template(request),
                "path": request.url.path,
                "status_code": status_code,
                "latency_ms": round(latency_ms, 2),
                "caller_id": principal.get("id"),
                "auth_type": principal.get("auth_type", "anonymous"),
                "roles": principal.get("roles") or [],
                "tenant_id": principal.get("tenant_id"),
                "client_ip": _client_ip(request),
            },
        )
    except Exception:  # auditing must never break request handling
        logger.exception("Failed to emit default request-audit event")
