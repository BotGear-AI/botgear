# unmanaged
"""Unit tests for script_service — the Script CRUD resource, plus its
divergent process_script_request action: "run" dispatches to
script_runner.run_in_background rather than the generic
process_*_request list-the-table pattern the other CRUD services use
(see test_generated_crud_services.py).
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from botgear.models.script import Script
from botgear.services import script_service as svc


@pytest.fixture
def wired_db(fake_db, monkeypatch):
    monkeypatch.setattr(svc, "get_database_client", lambda: fake_db)
    return fake_db


def _script(**overrides) -> Script:
    return Script(name="hello-world", **overrides)


# ─── standard CRUD contract (same shape as the generated services) ────

def test_create_generates_id_when_absent(wired_db):
    out = svc.create_script(_script(), user={}, request=None)
    assert out["id"]
    assert wired_db.get_item("script", out["id"]) == out


def test_create_keeps_explicit_id(wired_db):
    out = svc.create_script(_script(id="s1"), user={}, request=None)
    assert out["id"] == "s1"


def test_get_all_empty_then_populated(wired_db):
    assert svc.get_all_script(user={}, request=None) == []
    created = svc.create_script(_script(), user={}, request=None)
    assert svc.get_all_script(user={}, request=None) == [created]


def test_get_and_get_path_found_and_missing(wired_db):
    created = svc.create_script(_script(), user={}, request=None)
    assert svc.get_script(created["id"], user={}, request=None) == created
    assert svc.get_path_script(created["id"], user={}, request=None) == created
    assert svc.get_script("missing", user={}, request=None) is None
    assert svc.get_path_script("missing", user={}, request=None) is None


def test_update_existing_and_missing(wired_db):
    created = svc.create_script(_script(body="print(1)"), user={}, request=None)
    updated = Script(**{**created, "body": "print(2)"})
    out = svc.update_script(created["id"], updated, user={}, request=None)
    assert out["body"] == "print(2)"
    assert out == wired_db.get_item("script", created["id"])

    assert svc.update_script("missing", _script(), user={}, request=None) is None


def test_delete_existing_and_missing(wired_db):
    created = svc.create_script(_script(), user={}, request=None)
    out = svc.delete_script(created["id"], user={}, request=None)
    assert out == created
    assert wired_db.get_item("script", created["id"]) is None
    assert svc.delete_script("missing", user={}, request=None) is None


def test_no_database_client_short_circuits_every_verb(monkeypatch):
    monkeypatch.setattr(svc, "get_database_client", lambda: None)
    item = _script(id="x")
    assert svc.get_all_script(user={}, request=None) == []
    assert svc.get_script("x", user={}, request=None) is None
    assert svc.get_path_script("x", user={}, request=None) is None
    assert svc.update_script("x", item, user={}, request=None) is None
    assert svc.delete_script("x", user={}, request=None) is None
    created = svc.create_script(item, user={}, request=None)
    assert created["id"] == "x"  # still returned, just never persisted


# ─── process_script_request: action="run" ──────────────────────────────

def test_run_dispatches_to_script_runner_and_returns_its_run_id(wired_db, monkeypatch):
    created = svc.create_script(_script(body="print('hi')", language="python"), user={}, request=None)
    run_in_background = Mock(return_value="run-1")
    monkeypatch.setattr("botgear.runtime.script_runner.run_in_background", run_in_background)

    out = svc.process_script_request({"action": "run", "id": created["id"]}, user={}, request=None)

    run_in_background.assert_called_once_with(created["id"], "print('hi')", language="python")
    assert out["metadata"]["status"] == "success"
    assert out["metadata"]["run_id"] == "run-1"
    assert out["metadata"]["output_topic"] == f"/script/{created['id']}/output"
    assert out["records"] == []


def test_run_accepts_script_id_alias_for_id(wired_db, monkeypatch):
    created = svc.create_script(_script(), user={}, request=None)
    run_in_background = Mock(return_value="run-2")
    monkeypatch.setattr("botgear.runtime.script_runner.run_in_background", run_in_background)

    out = svc.process_script_request({"action": "run", "script_id": created["id"]}, user={}, request=None)
    assert out["metadata"]["run_id"] == "run-2"


def test_run_without_an_id_raises_400(wired_db):
    with pytest.raises(HTTPException) as exc_info:
        svc.process_script_request({"action": "run"}, user={}, request=None)
    assert exc_info.value.status_code == 400


def test_run_on_an_unknown_script_raises_404(wired_db):
    with pytest.raises(HTTPException) as exc_info:
        svc.process_script_request({"action": "run", "id": "ghost"}, user={}, request=None)
    assert exc_info.value.status_code == 404


def test_run_without_a_database_client_raises_404(monkeypatch):
    # db is None -> `script = db.get_item(...) if db else None` -> not found,
    # same 404 as a real lookup miss (no separate "no DB" branch here).
    monkeypatch.setattr(svc, "get_database_client", lambda: None)
    with pytest.raises(HTTPException) as exc_info:
        svc.process_script_request({"action": "run", "id": "s1"}, user={}, request=None)
    assert exc_info.value.status_code == 404


def test_run_propagates_a_bad_language_as_400(wired_db, monkeypatch):
    created = svc.create_script(_script(), user={}, request=None)
    monkeypatch.setattr(
        "botgear.runtime.script_runner.run_in_background",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("unsupported language")),
    )
    with pytest.raises(HTTPException) as exc_info:
        svc.process_script_request({"action": "run", "id": created["id"]}, user={}, request=None)
    assert exc_info.value.status_code == 400


def test_unknown_action_raises_400(wired_db):
    with pytest.raises(HTTPException) as exc_info:
        svc.process_script_request({"action": "lint"}, user={}, request=None)
    assert exc_info.value.status_code == 400
