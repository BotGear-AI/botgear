# unmanaged
"""Unit tests for service_request_service — the one generated CRUD module
that diverges from the shared template covered by
test_generated_crud_services.py: create_service_request stamps
status/created_at and dispatches to lifecycle.handle, rather than just
persisting the row as given.

lifecycle.handle is well covered on its own terms by test_lifecycle.py;
here it's monkeypatched to a stub so these tests stay a unit test of the
*wrapper* (persist → dispatch → return its result), not a re-test of the
state machine.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from botgear.models.service_request import ServiceRequest
from botgear.services import service_request_service as svc


@pytest.fixture
def wired_db(fake_db, monkeypatch):
    monkeypatch.setattr(svc, "get_database_client", lambda: fake_db)
    return fake_db


@pytest.fixture
def stub_handle(wired_db, monkeypatch):
    """Replaces lifecycle.handle with one that records its input, moves the
    row to a terminal state in wired_db (mirroring what the real handle()
    does via db.update_item — see lifecycle.py's own service_request
    writes) and returns that same terminal row, so tests can assert
    create_service_request both persisted the pre-dispatch row and
    returned whatever handle() came back with."""
    calls: list[Dict[str, Any]] = []

    def fake_handle(req: Dict[str, Any]) -> Dict[str, Any]:
        calls.append(dict(req))
        done = {**req, "status": "completed", "result": "ok"}
        wired_db.update_item("service_request", done["id"], done, include_nulls=True)
        return done

    monkeypatch.setattr(svc.lifecycle, "handle", fake_handle)
    return calls


def test_create_persists_pending_row_then_returns_handles_result(wired_db, stub_handle):
    item = ServiceRequest(action="start_service", service_proxy_id="cam-1")
    out = svc.create_service_request(item, user={}, request=None)

    # lifecycle.handle was given the pending, persisted row...
    assert len(stub_handle) == 1
    dispatched = stub_handle[0]
    assert dispatched["status"] == "pending"
    assert dispatched["action"] == "start_service"
    assert dispatched["created_at"]  # stamped, non-empty
    assert dispatched["id"]  # generated

    # ...and the service returns handle()'s result, not the pending row.
    assert out["status"] == "completed"
    assert out["result"] == "ok"
    assert out["id"] == dispatched["id"]


def test_create_generates_id_when_absent(wired_db, stub_handle):
    item = ServiceRequest(action="stop_service")
    out = svc.create_service_request(item, user={}, request=None)
    assert out["id"]


def test_create_keeps_explicit_id(wired_db, stub_handle):
    item = ServiceRequest(id="explicit-1", action="stop_service")
    out = svc.create_service_request(item, user={}, request=None)
    assert out["id"] == "explicit-1"


def test_create_without_db_skips_persist_and_dispatch(monkeypatch, stub_handle):
    # No DB configured: create_service_request neither persists nor runs
    # the request through lifecycle.handle — it just hands back the
    # stamped (but unsaved) row.
    monkeypatch.setattr(svc, "get_database_client", lambda: None)
    item = ServiceRequest(action="start_service")
    out = svc.create_service_request(item, user={}, request=None)
    assert out["status"] == "pending"
    assert stub_handle == []


def test_get_all_empty_then_populated(wired_db, stub_handle):
    assert svc.get_all_service_request(user={}, request=None) == []
    item = ServiceRequest(action="start_service")
    created = svc.create_service_request(item, user={}, request=None)
    assert svc.get_all_service_request(user={}, request=None) == [created]


def test_get_and_get_path_found_and_missing(wired_db, stub_handle):
    item = ServiceRequest(action="start_service")
    created = svc.create_service_request(item, user={}, request=None)
    assert svc.get_service_request(created["id"], user={}, request=None) == created
    assert svc.get_path_service_request(created["id"], user={}, request=None) == created
    assert svc.get_service_request("missing", user={}, request=None) is None
    assert svc.get_path_service_request("missing", user={}, request=None) is None


def test_update_existing_and_missing(wired_db, stub_handle):
    item = ServiceRequest(action="start_service")
    created = svc.create_service_request(item, user={}, request=None)
    updated_model = ServiceRequest(**{**created, "result": "manually edited"})
    out = svc.update_service_request(created["id"], updated_model, user={}, request=None)
    assert out["result"] == "manually edited"
    assert out == wired_db.get_item("service_request", created["id"])

    missing = ServiceRequest(action="start_service")
    assert svc.update_service_request("missing", missing, user={}, request=None) is None


def test_delete_existing_and_missing(wired_db, stub_handle):
    item = ServiceRequest(action="start_service")
    created = svc.create_service_request(item, user={}, request=None)
    out = svc.delete_service_request(created["id"], user={}, request=None)
    assert out == created
    assert wired_db.get_item("service_request", created["id"]) is None
    assert svc.delete_service_request("missing", user={}, request=None) is None


def test_process_request_lists_rows(wired_db, stub_handle):
    item = ServiceRequest(action="start_service")
    created = svc.create_service_request(item, user={}, request=None)
    out = svc.process_service_request_request({}, user={}, request=None)
    assert out["metadata"]["status"] == "success"
    assert out["records"] == [created]


def test_no_database_client_short_circuits_reads(monkeypatch, stub_handle):
    monkeypatch.setattr(svc, "get_database_client", lambda: None)
    item = ServiceRequest(id="x", action="start_service")
    assert svc.get_all_service_request(user={}, request=None) == []
    assert svc.get_service_request("x", user={}, request=None) is None
    assert svc.get_path_service_request("x", user={}, request=None) is None
    assert svc.update_service_request("x", item, user={}, request=None) is None
    assert svc.delete_service_request("x", user={}, request=None) is None
