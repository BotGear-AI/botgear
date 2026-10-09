# unmanaged
"""Unit tests for api_client_service — the ApiClient CRUD + the
X-API-Key -> principal resolver (validate_api_client_secret) the auth
package calls into. Unlike the generated-template services, this one is
hand-written with real security-relevant behavior (secret minting,
exclude_unset partial updates, never leaking the secret back out of
validate_api_client_secret) — see its own module docstring.
"""
from __future__ import annotations

import pytest

from botgear.models.api_client import ApiClient
from botgear.services import api_client_service as svc


@pytest.fixture
def wired_db(fake_db, monkeypatch):
    monkeypatch.setattr(svc, "get_database_client", lambda: fake_db)
    return fake_db


# ─── create_api_client ───────────────────────────────────────────────

def test_create_mints_a_secret_and_created_timestamp_when_absent(wired_db):
    out = svc.create_api_client(ApiClient(name="Reporting"), user={}, request=None)
    assert out["id"]
    assert out["secret"]  # non-empty, server-minted
    assert isinstance(out["created"], int) and out["created"] > 0
    assert wired_db.get_item("api_client", out["id"]) == out


def test_create_keeps_an_explicit_secret_and_created(wired_db):
    out = svc.create_api_client(
        ApiClient(name="Reporting", secret="my-own-secret", created=1000), user={}, request=None,
    )
    assert out["secret"] == "my-own-secret"
    assert out["created"] == 1000


def test_create_keeps_explicit_id(wired_db):
    out = svc.create_api_client(ApiClient(id="client-1", name="x"), user={}, request=None)
    assert out["id"] == "client-1"


def test_create_without_db_still_returns_the_row_unpersisted(monkeypatch):
    monkeypatch.setattr(svc, "get_database_client", lambda: None)
    out = svc.create_api_client(ApiClient(name="x"), user={}, request=None)
    assert out["secret"]  # minting happens before the db check


# ─── get_all / get ──────────────────────────────────────────────────

def test_get_all_empty_then_populated(wired_db):
    assert svc.get_all_api_client(user={}, request=None) == []
    created = svc.create_api_client(ApiClient(name="x"), user={}, request=None)
    assert svc.get_all_api_client(user={}, request=None) == [created]


def test_get_found_and_missing(wired_db):
    created = svc.create_api_client(ApiClient(name="x"), user={}, request=None)
    assert svc.get_api_client(created["id"], user={}, request=None) == created
    assert svc.get_api_client("missing", user={}, request=None) is None


def test_get_all_and_get_without_db(monkeypatch):
    monkeypatch.setattr(svc, "get_database_client", lambda: None)
    assert svc.get_all_api_client(user={}, request=None) == []
    assert svc.get_api_client("x", user={}, request=None) is None


# ─── update_api_client — exclude_unset partial-update semantics ────────

def test_update_only_writes_fields_the_caller_actually_set(wired_db):
    created = svc.create_api_client(ApiClient(name="x", roles=["Admin"]), user={}, request=None)
    # A rename-only PUT — roles is NOT in the payload, so model_fields_set
    # doesn't include it even though the model's default is [] (not None).
    rename_only = ApiClient(name="renamed")
    out = svc.update_api_client(created["id"], rename_only, user={}, request=None)
    assert out["name"] == "renamed"
    assert out["roles"] == ["Admin"]  # NOT wiped to [] by the omitted default


def test_update_an_explicit_empty_roles_list_does_clear_it(wired_db):
    created = svc.create_api_client(ApiClient(name="x", roles=["Admin"]), user={}, request=None)
    out = svc.update_api_client(created["id"], ApiClient(name="x", roles=[]), user={}, request=None)
    assert out["roles"] == []


def test_update_missing_row_returns_none(wired_db):
    assert svc.update_api_client("missing", ApiClient(name="x"), user={}, request=None) is None


def test_update_without_db_returns_none(monkeypatch):
    monkeypatch.setattr(svc, "get_database_client", lambda: None)
    assert svc.update_api_client("x", ApiClient(name="x"), user={}, request=None) is None


# ─── delete_api_client ──────────────────────────────────────────────

def test_delete_existing_and_missing(wired_db):
    created = svc.create_api_client(ApiClient(name="x"), user={}, request=None)
    assert svc.delete_api_client(created["id"], user={}, request=None) == created
    assert wired_db.get_item("api_client", created["id"]) is None
    assert svc.delete_api_client("missing", user={}, request=None) is None


def test_delete_without_db_returns_none(monkeypatch):
    monkeypatch.setattr(svc, "get_database_client", lambda: None)
    assert svc.delete_api_client("x", user={}, request=None) is None


# ─── validate_api_client_secret ─────────────────────────────────────

def test_validate_empty_secret_returns_none_without_a_db_lookup(wired_db):
    assert svc.validate_api_client_secret("") is None
    assert svc.validate_api_client_secret(None) is None


def test_validate_raises_when_the_database_is_unavailable(monkeypatch):
    monkeypatch.setattr(svc, "get_database_client", lambda: None)
    with pytest.raises(RuntimeError):
        svc.validate_api_client_secret("some-secret")


def test_validate_no_match_returns_none(wired_db):
    assert svc.validate_api_client_secret("not-a-real-secret") is None


def test_validate_match_builds_a_principal_without_leaking_the_secret(wired_db):
    svc.create_api_client(ApiClient(name="Reporting", secret="shh", roles=["Admin"]), user={}, request=None)
    principal = svc.validate_api_client_secret("shh")
    assert principal is not None
    assert "secret" not in principal
    assert principal["roles"] == ["Admin"]
    assert principal["auth_type"] == "shared_secret"
    assert principal["name"] == "Reporting"


def test_validate_defaults_roles_to_empty_list_when_the_row_has_none(wired_db):
    # Direct DB insert — bypasses create_api_client's own defaulting, to
    # exercise validate_api_client_secret's own `setdefault("roles", [])`.
    wired_db.insert_item("api_client", "c1", {"id": "c1", "name": "x", "secret": "shh"})
    principal = svc.validate_api_client_secret("shh")
    assert principal["roles"] == []
