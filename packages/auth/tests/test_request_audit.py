"""Tests for auth.request_audit.emit_default_audit_event."""
import logging
from types import SimpleNamespace

from fastapi import FastAPI, Request

from auth import request_audit
from auth.request_audit import DEFAULT_AUDIT_LOG_TYPE, emit_default_audit_event


def _request(headers=None, client=("10.0.0.9", 1234), route=None, principal=None, path="/v1/items/42"):
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
        "server": ("testserver", 80),
        "scheme": "http",
    }
    if route is not None:
        scope["route"] = route
    request = Request(scope)
    if principal is not None:
        request.state.audit_principal = principal
    return request


def _emitted(caplog):
    records = [r for r in caplog.records if r.name == "auth.request_audit"]
    assert len(records) == 1
    return records[0]


def test_emits_principal_fields_and_route_template(caplog):
    request = _request(
        route=SimpleNamespace(path="/v1/items/{item_id}"),
        principal={"id": "u1", "auth_type": "jwt", "roles": ["admin"], "tenant_id": "t1"},
    )
    with caplog.at_level(logging.INFO, logger="auth.request_audit"):
        emit_default_audit_event(request, 201, 12.3456)

    rec = _emitted(caplog)
    assert rec.levelno == logging.INFO
    assert rec.log_type == DEFAULT_AUDIT_LOG_TYPE
    assert rec.method == "GET"
    assert rec.route == "/v1/items/{item_id}"
    assert rec.path == "/v1/items/42"
    assert rec.status_code == 201
    assert rec.latency_ms == 12.35
    assert rec.caller_id == "u1"
    assert rec.auth_type == "jwt"
    assert rec.roles == ["admin"]
    assert rec.tenant_id == "t1"
    assert rec.client_ip == "10.0.0.9"
    assert isinstance(rec.timestamp, int)


def test_anonymous_request_defaults(caplog):
    # No principal, no matched route, no client: every fallback arm.
    request = _request(client=None)
    with caplog.at_level(logging.INFO, logger="auth.request_audit"):
        emit_default_audit_event(request, 404, 1.0)

    rec = _emitted(caplog)
    assert rec.route == "/v1/items/42"
    assert rec.caller_id is None
    assert rec.auth_type == "anonymous"
    assert rec.roles == []
    assert rec.tenant_id is None
    assert rec.client_ip is None


def test_forwarded_for_takes_first_hop(caplog):
    request = _request(headers={"X-Forwarded-For": " 203.0.113.7 , 10.0.0.1"})
    with caplog.at_level(logging.INFO, logger="auth.request_audit"):
        emit_default_audit_event(request, 200, 0.5)
    assert _emitted(caplog).client_ip == "203.0.113.7"


def test_route_without_path_falls_back_to_url_path(caplog):
    request = _request(route=SimpleNamespace(path=None))
    with caplog.at_level(logging.INFO, logger="auth.request_audit"):
        emit_default_audit_event(request, 200, 0.5)
    assert _emitted(caplog).route == "/v1/items/42"


def test_never_raises_on_bad_principal(caplog):
    # A principal without .get() blows up inside the try; the event must be
    # swallowed and logged, not propagated into request handling.
    request = _request(principal=["not", "a", "dict"])
    with caplog.at_level(logging.INFO, logger="auth.request_audit"):
        emit_default_audit_event(request, 500, 3.0)

    records = [r for r in caplog.records if r.name == "auth.request_audit"]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert "Failed to emit default request-audit event" in records[0].getMessage()
    assert records[0].exc_info is not None


def test_route_template_from_real_api_route():
    # A real APIRoute (what FastAPI puts in scope["route"]) exposes the template.
    app = FastAPI()

    @app.get("/v1/things/{thing_id}")
    def get_thing(thing_id: str):
        return {"id": thing_id}

    route = next(r for r in app.routes if getattr(r, "path", None) == "/v1/things/{thing_id}")
    request = _request(route=route, path="/v1/things/abc")
    assert request_audit._route_template(request) == "/v1/things/{thing_id}"
