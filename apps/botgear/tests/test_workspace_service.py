# unmanaged
"""Unit tests for workspace_service — the Workspace CRUD resource, plus
the behavior that sets it apart from the generated-template services
(see test_generated_crud_services.py):

  * _hydrate_runtime_membership: a 'runtime'-kind workspace's
    service_proxy_ids is DERIVED from the live service_proxy table on
    every read, not stored.
  * update_workspace forces kind/service_proxy_ids back for a runtime
    workspace regardless of what the caller's payload says.
  * delete_workspace refuses to delete the runtime workspace.
"""
from __future__ import annotations

import pytest

from botgear.models.workspace import Workspace
from botgear.services import workspace_service as svc


@pytest.fixture
def wired_db(fake_db, monkeypatch):
    monkeypatch.setattr(svc, "get_database_client", lambda: fake_db)
    return fake_db


def _proxy(db, id: str, status: str) -> None:
    db.insert_item("service_proxy", id, {"id": id, "status": status})


# ─── basic CRUD (kind='user' — no hydration kicks in) ──────────────────

def test_create_generates_id_when_absent(wired_db):
    out = svc.create_workspace(Workspace(name="w1"), user={}, request=None)
    assert out["id"]
    assert wired_db.get_item("workspace", out["id"]) == out


def test_get_all_empty_then_populated(wired_db):
    assert svc.get_all_workspace(user={}, request=None) == []
    created = svc.create_workspace(Workspace(name="w1"), user={}, request=None)
    assert svc.get_all_workspace(user={}, request=None) == [created]


def test_get_and_get_path_found_and_missing(wired_db):
    created = svc.create_workspace(Workspace(name="w1"), user={}, request=None)
    assert svc.get_workspace(created["id"], user={}, request=None) == created
    assert svc.get_path_workspace(created["id"], user={}, request=None) == created
    assert svc.get_workspace("missing", user={}, request=None) is None
    assert svc.get_path_workspace("missing", user={}, request=None) is None


def test_update_existing_and_missing(wired_db):
    created = svc.create_workspace(Workspace(name="w1", description="before"), user={}, request=None)
    updated = Workspace(**{**created, "description": "after"})
    out = svc.update_workspace(created["id"], updated, user={}, request=None)
    assert out["description"] == "after"
    assert svc.update_workspace("missing", Workspace(name="w1"), user={}, request=None) is None


def test_delete_existing_and_missing(wired_db):
    created = svc.create_workspace(Workspace(name="w1"), user={}, request=None)
    assert svc.delete_workspace(created["id"], user={}, request=None) == created
    assert wired_db.get_item("workspace", created["id"]) is None
    assert svc.delete_workspace("missing", user={}, request=None) is None


def test_no_database_client_short_circuits_every_verb(monkeypatch):
    monkeypatch.setattr(svc, "get_database_client", lambda: None)
    item = Workspace(id="x", name="w1")
    assert svc.get_all_workspace(user={}, request=None) == []
    assert svc.get_workspace("x", user={}, request=None) is None
    assert svc.get_path_workspace("x", user={}, request=None) is None
    assert svc.update_workspace("x", item, user={}, request=None) is None
    assert svc.delete_workspace("x", user={}, request=None) is None
    created = svc.create_workspace(item, user={}, request=None)
    assert created["id"] == "x"


# ─── _hydrate_runtime_membership (kind='runtime') ───────────────────────

def test_runtime_workspace_membership_is_derived_from_service_proxy(wired_db):
    svc.create_workspace(Workspace(id="runtime", name="Runtime", kind="runtime"), user={}, request=None)
    _proxy(wired_db, "cam-1", "running")
    _proxy(wired_db, "arm-1", "placeholder")
    _proxy(wired_db, "old-1", "uninstalled")  # not a canvas state -> excluded

    out = svc.get_workspace("runtime", user={}, request=None)
    assert set(out["service_proxy_ids"]) == {"cam-1", "arm-1"}


def test_runtime_workspace_membership_updates_live_across_reads(wired_db):
    svc.create_workspace(Workspace(id="runtime", name="Runtime", kind="runtime"), user={}, request=None)
    assert svc.get_workspace("runtime", user={}, request=None)["service_proxy_ids"] == []
    _proxy(wired_db, "cam-1", "starting")
    assert svc.get_workspace("runtime", user={}, request=None)["service_proxy_ids"] == ["cam-1"]


def test_get_all_hydrates_every_runtime_workspace_row(wired_db):
    svc.create_workspace(Workspace(id="runtime", name="Runtime", kind="runtime"), user={}, request=None)
    svc.create_workspace(Workspace(id="saved-1", name="Saved", kind="user"), user={}, request=None)
    _proxy(wired_db, "cam-1", "running")

    rows = {r["id"]: r for r in svc.get_all_workspace(user={}, request=None)}
    assert rows["runtime"]["service_proxy_ids"] == ["cam-1"]
    assert rows["saved-1"]["service_proxy_ids"] is None  # untouched — not a runtime kind


def test_get_path_also_hydrates(wired_db):
    svc.create_workspace(Workspace(id="runtime", name="Runtime", kind="runtime"), user={}, request=None)
    _proxy(wired_db, "cam-1", "error")  # error is still a visible canvas state
    out = svc.get_path_workspace("runtime", user={}, request=None)
    assert out["service_proxy_ids"] == ["cam-1"]


# ─── update_workspace forcing kind/service_proxy_ids for 'runtime' ──────

def test_update_on_the_runtime_workspace_ignores_a_caller_supplied_kind_and_membership(wired_db):
    svc.create_workspace(Workspace(id="runtime", name="Runtime", kind="runtime"), user={}, request=None)
    _proxy(wired_db, "cam-1", "running")

    # Caller tries to demote it to a plain workspace and hand-pick members —
    # both are the runtime's own business, not the API client's.
    sneaky = Workspace(id="runtime", name="Runtime", kind="user", service_proxy_ids=["not-real"])
    out = svc.update_workspace("runtime", sneaky, user={}, request=None)

    assert out["kind"] == "runtime"
    assert out["service_proxy_ids"] == ["cam-1"]  # derived, not the caller's list


def test_update_on_a_user_workspace_keeps_the_callers_membership(wired_db):
    svc.create_workspace(Workspace(id="w1", name="Saved", kind="user"), user={}, request=None)
    updated = Workspace(id="w1", name="Saved", kind="user", service_proxy_ids=["a", "b"])
    out = svc.update_workspace("w1", updated, user={}, request=None)
    assert out["service_proxy_ids"] == ["a", "b"]


# ─── delete_workspace refuses the runtime singleton ─────────────────────

def test_delete_refuses_the_runtime_workspace(wired_db):
    svc.create_workspace(Workspace(id="runtime", name="Runtime", kind="runtime"), user={}, request=None)
    out = svc.delete_workspace("runtime", user={}, request=None)
    assert out is None
    assert wired_db.get_item("workspace", "runtime") is not None  # still there


def test_delete_allows_a_user_workspace(wired_db):
    svc.create_workspace(Workspace(id="w1", name="Saved", kind="user"), user={}, request=None)
    out = svc.delete_workspace("w1", user={}, request=None)
    assert out is not None
    assert wired_db.get_item("workspace", "w1") is None


# ─── process_workspace_request ──────────────────────────────────────────
# Note: like every generated service's process_*_request, this calls
# get_settings() unconditionally; it only succeeds once something else in
# the process has called create_app_settings() — true for the full suite
# (see test_generated_crud_services.py's identical case), not necessarily
# this one file run alone.

def test_process_request_lists_rows_from_the_table(wired_db):
    created = svc.create_workspace(Workspace(name="w1"), user={}, request=None)
    out = svc.process_workspace_request({}, user={}, request=None)
    assert out["metadata"]["status"] == "success"
    assert out["records"] == [created]
