# unmanaged
"""Unit tests for the generated-from-the-same-template CRUD service modules.

``config_service``, ``registration_service``, ``service_config_service``,
``service_meta_service``, ``service_proxy_service`` and ``user_service`` are
all hand-written (``# unmanaged``) but share one shape — the per-model CRUD
wrapper pattern the generator's route layer calls into
(create/get_all/get/get_path/update/delete/process_request). Rather than six
near-identical copy-pasted test files, this parametrizes the shared contract
once across all six and monkeypatches each module's own
``get_database_client`` to a shared FakeDB (see conftest.py).

``service_request_service`` follows the same shape for its read/update/
delete/process methods but its ``create`` also dispatches to
``lifecycle.handle`` — covered separately in test_service_request_service.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Type

import pytest
from pydantic import BaseModel

from botgear.models.config import Config
from botgear.models.registration import Registration
from botgear.models.service_config import ServiceConfig
from botgear.models.service_meta import ServiceMeta
from botgear.models.service_proxy import ServiceProxy
from botgear.models.user import User
from botgear.services import (
    config_service,
    registration_service,
    service_config_service,
    service_meta_service,
    service_proxy_service,
    user_service,
)


@dataclass(frozen=True)
class CrudModule:
    module: Any
    table: str
    model: Type[BaseModel]
    make_kwargs: Callable[[], Dict[str, Any]]
    verb: str  # e.g. "config" in create_config/get_config/...
    # One field (name, "before" value, "after" value) every case can exercise
    # in the update test, proving a change actually persists rather than
    # just round-tripping the same row.
    mutate_field: tuple
    # ServiceMeta has no `id` field — its key is minted fresh on every
    # create() regardless of input (its real identity is name@version,
    # assigned elsewhere). False skips the "caller sets the id" tests.
    has_id: bool = True

    def fn(self, name: str):
        return getattr(self.module, f"{name}_{self.verb}")


CASES = [
    CrudModule(config_service, "config", Config, dict, "config", ("port", 8998, 9000)),
    CrudModule(registration_service, "registration", Registration, dict, "registration", ("fullname", None, "Ada Lovelace")),
    CrudModule(
        service_config_service, "service_config", ServiceConfig,
        lambda: {"service_proxy_id": "cam-1", "service_meta_id": "opencv@4.8.0"},
        "service_config", ("params", None, {"fps": 30}),
    ),
    CrudModule(
        service_meta_service, "service_meta", ServiceMeta,
        lambda: {"name": "camera"},
        "service_meta", ("description", None, "A camera"), has_id=False,
    ),
    CrudModule(
        service_proxy_service, "service_proxy", ServiceProxy,
        lambda: {"name": "cam-1", "service_meta_id": "opencv@4.8.0"},
        "service_proxy", ("status", "stopped", "running"),
    ),
    CrudModule(user_service, "user", User, dict, "user", ("fullname", None, "Ada Lovelace")),
]
CASE_IDS = [c.table for c in CASES]


@pytest.fixture
def wired_db(fake_db, monkeypatch, case: CrudModule):
    """Points ``case.module``'s ``get_database_client`` at ``fake_db``."""
    monkeypatch.setattr(case.module, "get_database_client", lambda: fake_db)
    return fake_db


# `case` is requested directly by tests (not just by the `wired_db` fixture
# above) so each test can build its own request/model instances.
@pytest.fixture(params=CASES, ids=CASE_IDS)
def case(request) -> CrudModule:
    return request.param


def test_create_generates_id_when_absent(case, wired_db):
    item = case.model(**case.make_kwargs())
    out = case.fn("create")(item, user={}, request=None)
    assert out["id"]
    assert wired_db.get_item(case.table, out["id"]) == out


def test_create_keeps_explicit_id(case, wired_db):
    if not case.has_id:
        pytest.skip(f"{case.table} has no settable id field")
    item = case.model(id="explicit-1", **case.make_kwargs())
    out = case.fn("create")(item, user={}, request=None)
    assert out["id"] == "explicit-1"
    assert wired_db.get_item(case.table, "explicit-1") == out


def test_get_all_empty_then_populated(case, wired_db):
    assert case.fn("get_all")(user={}, request=None) == []
    item = case.model(**case.make_kwargs())
    created = case.fn("create")(item, user={}, request=None)
    assert case.fn("get_all")(user={}, request=None) == [created]


def test_get_found_and_missing(case, wired_db):
    item = case.model(**case.make_kwargs())
    created = case.fn("create")(item, user={}, request=None)
    assert case.fn("get")(created["id"], user={}, request=None) == created
    assert case.fn("get")("does-not-exist", user={}, request=None) is None


def test_get_path_reads_by_the_same_key_as_get(case, wired_db):
    # get_path_X takes a `path` positional but looks it up the same way
    # get_X does — see e.g. config_service.get_path_config.
    item = case.model(**case.make_kwargs())
    created = case.fn("create")(item, user={}, request=None)
    assert case.fn("get_path")(created["id"], user={}, request=None) == created


def test_update_existing_row(case, wired_db):
    field, before, after = case.mutate_field
    item = case.model(**case.make_kwargs())
    created = case.fn("create")(item, user={}, request=None)
    assert created[field] == before

    updated_model = case.model(**{**created, field: after})
    out = case.fn("update")(created["id"], updated_model, user={}, request=None)

    assert out[field] == after
    assert out == wired_db.get_item(case.table, created["id"])


def test_update_missing_row_returns_none(case, wired_db):
    # update_X(id, ...) looks the row up by its own `id` arg, independent of
    # whatever the body model carries — a literal missing key exercises that
    # without needing the model to have a settable `id` field (ServiceMeta
    # doesn't).
    missing_key = "missing-xyz"
    item = case.model(**case.make_kwargs())
    out = case.fn("update")(missing_key, item, user={}, request=None)
    assert out is None
    # And nothing was created as a side effect of the "update".
    assert wired_db.get_item(case.table, missing_key) is None


def test_delete_existing_and_missing(case, wired_db):
    item = case.model(**case.make_kwargs())
    created = case.fn("create")(item, user={}, request=None)
    out = case.fn("delete")(created["id"], user={}, request=None)
    assert out == created
    assert wired_db.get_item(case.table, created["id"]) is None
    # Deleting again (now missing) doesn't raise and reports nothing found.
    assert case.fn("delete")(created["id"], user={}, request=None) is None


def test_process_request_lists_rows_from_the_table(case, wired_db):
    item = case.model(**case.make_kwargs())
    created = case.fn("create")(item, user={}, request=None)
    process = getattr(case.module, f"process_{case.verb}_request")
    out = process({}, user={}, request=None)
    assert out["metadata"]["status"] == "success"
    assert out["records"] == [created]


def test_no_database_client_short_circuits_every_verb(case, monkeypatch):
    """When get_database_client() returns None (no DB configured yet,
    e.g. very early boot), every read/write is a no-op rather than an
    AttributeError/UnboundLocalError on a None db. (get_X/get_path_X/
    delete_X all used to crash this way — see test_generated_crud_services'
    module docstring history / the fix in services/*_service.py.)"""
    monkeypatch.setattr(case.module, "get_database_client", lambda: None)
    kwargs = {"id": "x", **case.make_kwargs()} if case.has_id else case.make_kwargs()
    item = case.model(**kwargs)

    assert case.fn("get_all")(user={}, request=None) == []
    assert case.fn("get")("x", user={}, request=None) is None
    assert case.fn("get_path")("x", user={}, request=None) is None
    assert case.fn("update")("x", item, user={}, request=None) is None
    assert case.fn("delete")("x", user={}, request=None) is None
    # create still returns the item it was given (just never persisted).
    created = case.fn("create")(item, user={}, request=None)
    if case.has_id:
        assert created["id"] == "x"
