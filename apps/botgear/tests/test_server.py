# unmanaged
"""Unit tests for server.py's AppServer — the custom auth surface
(login, MFA, forgot/reset-password, accept-invite, refresh-token,
logout, register/verify, resolve-auth) plus its handful of pure
helpers. The generated CRUD routers it mounts (config/user/service_*/
workspace/peer/...) are already covered by their own service-layer
tests; this file is about AppServer's own code.

AppServer's __init__ constructs a real auth provider
(auth.factory.get_auth_provider -> LocalAuthProvider for the default
auth_type="local"). LocalAuthProvider is a process-wide SINGLETON that
captures `get_database_client()` once, at first construction — so the
`app_server` fixture resets that singleton and monkeypatches the
global factory function (not just this app's own Depends-injected db)
BEFORE constructing AppServer, and resets the singleton back to None
afterward so later test files aren't left holding this test's FakeDB.

Scope intentionally excludes: OAuth login/callback/logout (needs a
real IdP), the SPA fallback and static asset mounting (needs a real
UI build directory), and the WS/stream/script/admin-state/bus/registry/
update/brain-files routers mounted via register_*_routes/include_router
(each has — or will have — its own test file; re-testing them through
AppServer would just be indirection).

KNOWN BUG, not fixed here: /v1/mfa/setup and /v1/mfa/qrcode's success
path does `self.settings.base_url` — base_url is not a declared field
on Config/ConfigBase anywhere in the monorepo, so a real request past
the auth/lookup checks raises AttributeError (-> 500). server.py is
`# managed` (generated from templates/apps/botgear.yml); fixing this
means fixing the template (and regenerating every app that shares it),
not patching the generated output directly — out of scope for a
coverage pass. The 401/404 paths below (reachable before that line) are
real, correct behavior and are tested.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict

import jwt as _pyjwt
import pytest
from auth import get_jwt_secret
from fastapi.testclient import TestClient

import botgear.server as server_mod
from botgear.models.config import Config as BotgearConfig
from botgear.server import (
    AppServer,
    _delete_active_sessions_for_identity,
    build_oauth_state,
    decode_oauth_state,
    hash_refresh_token,
    load_version,
    session_client_metadata,
)


# ─── pure helpers — no app needed ───────────────────────────────────────

def test_hash_refresh_token_is_deterministic_and_secret_dependent():
    a = hash_refresh_token("tok-1", secret="s1")
    b = hash_refresh_token("tok-1", secret="s1")
    c = hash_refresh_token("tok-1", secret="s2")
    assert a == b
    assert a != c
    assert a.startswith("sha256:")


def test_hash_refresh_token_resolves_the_secret_when_omitted(monkeypatch):
    monkeypatch.setattr("auth.get_jwt_secret", lambda: "resolved-secret")
    assert hash_refresh_token("tok-1") == hash_refresh_token("tok-1", secret="resolved-secret")


def test_load_version_falls_back_when_the_file_is_absent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no version.json here
    assert load_version() == {"version": "0.0.0"}


def test_session_client_metadata_prefers_x_forwarded_for():
    class FakeRequest:
        headers = {"x-forwarded-for": "1.2.3.4, 5.6.7.8", "user-agent": "pytest"}
        client = None

    out = session_client_metadata(FakeRequest())
    assert out == {"user_agent": "pytest", "ip_address": "1.2.3.4"}


def test_session_client_metadata_falls_back_to_socket_peer():
    class FakePeer:
        host = "9.9.9.9"

    class FakeRequest:
        headers = {}
        client = FakePeer()

    assert session_client_metadata(FakeRequest())["ip_address"] == "9.9.9.9"


def test_session_client_metadata_handles_a_missing_request():
    assert session_client_metadata(None) == {"user_agent": None, "ip_address": None}


def test_delete_active_sessions_for_identity(fake_db):
    fake_db.insert_item("auth_session", "s1", {"id": "s1", "user_id": "u1", "status": "active"})
    fake_db.insert_item("auth_session", "s2", {"id": "s2", "user_id": "u1", "status": "active"})
    fake_db.insert_item("auth_session", "s3", {"id": "s3", "user_id": "u1", "status": "expired"})
    fake_db.insert_item("auth_session", "s4", {"id": "s4", "user_id": "other", "status": "active"})

    deleted = _delete_active_sessions_for_identity(fake_db, "U1")  # case-insensitive
    assert deleted == 2
    assert fake_db.get_item("auth_session", "s1") is None
    assert fake_db.get_item("auth_session", "s2") is None
    assert fake_db.get_item("auth_session", "s3") is not None  # not active -> untouched
    assert fake_db.get_item("auth_session", "s4") is not None  # different user -> untouched


def test_delete_active_sessions_for_identity_with_no_db_or_identity_is_a_safe_noop(fake_db):
    assert _delete_active_sessions_for_identity(None, "u1") == 0
    assert _delete_active_sessions_for_identity(fake_db, None) == 0
    assert _delete_active_sessions_for_identity(fake_db, "") == 0


def test_oauth_state_round_trips():
    state = build_oauth_state("user-1", sso_config_id="tenant-a")
    decoded = decode_oauth_state(state)
    assert decoded["user_id"] == "user-1"
    assert decoded["sso_config_id"] == "tenant-a"
    assert "nonce" in decoded


def test_oauth_state_omits_absent_optional_fields():
    decoded = decode_oauth_state(build_oauth_state(None))
    assert "user_id" not in decoded
    assert "sso_config_id" not in decoded


def test_decode_oauth_state_handles_missing_and_malformed_input():
    assert decode_oauth_state(None) is None
    assert decode_oauth_state("not-valid-base64!!") is None


# ─── AppServer + TestClient ─────────────────────────────────────────────

def _password_hash(password: str) -> str:
    from auth import password_util
    return password_util.hash_password(password)


@pytest.fixture
def app_server(monkeypatch, fake_db, tmp_path):
    # database.factory.get_database_client for anything that calls it
    # qualified (server.py's own routes, via Depends(get_db_provider)
    # override below anyway) — but auth.local_auth did
    # `from database.factory import get_database_client`, a bare-name
    # import into ITS OWN module namespace, so patching the source alone
    # doesn't reach it; LocalAuthProvider's internal self.database needs
    # its own patch target.
    monkeypatch.setattr("database.factory.get_database_client", lambda: fake_db)
    monkeypatch.setattr("auth.local_auth.get_database_client", lambda: fake_db)
    import auth.local_auth as local_auth_mod
    local_auth_mod.LocalAuthProvider._instance = None
    try:
        settings = BotgearConfig(data_dir=str(tmp_path))  # auth_bootstrap defaults to first_user_claim
        server = AppServer(settings)
        server.app.dependency_overrides[server.get_db_provider] = lambda: fake_db
        yield server
    finally:
        local_auth_mod.LocalAuthProvider._instance = None


@pytest.fixture
def client(app_server) -> TestClient:
    return TestClient(app_server.app)


def _seed_user(fake_db, user_id="user@example.com", password="correct-password", **extra) -> Dict[str, Any]:
    # email is required: mfa_login/refresh_token mint a JWT straight from
    # this dict (self.auth.generate_token(user, ...) -> mint_jwt), unlike
    # the plain /v1/login path, which looks the user up by id through
    # authenticate() -> _generate_jwt(user_id), which synthesizes a
    # missing email from the id itself.
    row = {
        "id": user_id, "email": user_id, "password_hash": _password_hash(password),
        "status": "active", "roles": ["User"], **extra,
    }
    fake_db.insert_item("user", user_id, row)
    return row


def test_version(client):
    assert client.get("/v1/version").json() == load_version()


# ─── /v1/login ───────────────────────────────────────────────────────

def test_login_success_returns_an_access_and_refresh_token(client, fake_db):
    _seed_user(fake_db)
    resp = client.post("/v1/login", json={"username": "user@example.com", "password": "correct-password"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["token_type"] == "bearer"
    # A session row was created for the refresh token issued.
    sessions = fake_db.query_items("auth_session", {"user_id": "user@example.com"})
    assert len(sessions) == 1
    assert sessions[0]["refresh_token_hash"] == hash_refresh_token(body["refresh_token"])


def test_login_is_case_insensitive_on_username(client, fake_db):
    _seed_user(fake_db, user_id="user@example.com")
    resp = client.post("/v1/login", json={"username": "User@Example.com", "password": "correct-password"})
    assert resp.status_code == 200


def test_login_wrong_password_is_rejected(client, fake_db):
    _seed_user(fake_db)
    resp = client.post("/v1/login", json={"username": "user@example.com", "password": "nope"})
    assert resp.status_code == 401


def test_login_unknown_user_is_rejected(client):
    resp = client.post("/v1/login", json={"username": "ghost@example.com", "password": "x"})
    assert resp.status_code == 401


def test_login_missing_credentials_is_rejected(client):
    resp = client.post("/v1/login", json={"username": "", "password": ""})
    assert resp.status_code == 401


def test_login_mfa_enabled_returns_mfa_required_instead_of_a_token(client, fake_db):
    _seed_user(fake_db, is_mfa_enabled=True, totp_secret="ABCDEF")
    resp = client.post("/v1/login", json={"username": "user@example.com", "password": "correct-password"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mfa_required"] is True
    assert not body.get("access_token")


@pytest.mark.parametrize("status,expected_code", [("invited", 403), ("disabled", 403), ("locked", 403)])
def test_login_blocks_non_active_account_statuses(client, fake_db, status, expected_code):
    _seed_user(fake_db, status=status)
    resp = client.post("/v1/login", json={"username": "user@example.com", "password": "correct-password"})
    assert resp.status_code == expected_code


# ─── /v1/mfa/login ───────────────────────────────────────────────────

def test_mfa_login_success(client, fake_db):
    import pyotp
    secret = pyotp.random_base32()
    _seed_user(fake_db, totp_secret=secret, is_mfa_enabled=True)
    code = pyotp.TOTP(secret).now()
    resp = client.post("/v1/mfa/login", json={"username": "user@example.com", "code": code})
    assert resp.status_code == 200
    assert resp.json()["access_token"]


def test_mfa_login_not_set_up(client, fake_db):
    _seed_user(fake_db)
    resp = client.post("/v1/mfa/login", json={"username": "user@example.com", "code": "000000"})
    assert resp.status_code == 400


def test_mfa_login_wrong_code(client, fake_db):
    import pyotp
    _seed_user(fake_db, totp_secret=pyotp.random_base32(), is_mfa_enabled=True)
    resp = client.post("/v1/mfa/login", json={"username": "user@example.com", "code": "000000"})
    assert resp.status_code == 401


@pytest.mark.parametrize("status", ["invited", "disabled", "locked"])
def test_mfa_login_blocks_non_active_statuses(client, fake_db, status):
    import pyotp
    secret = pyotp.random_base32()
    _seed_user(fake_db, totp_secret=secret, is_mfa_enabled=True, status=status)
    resp = client.post("/v1/mfa/login", json={"username": "user@example.com", "code": pyotp.TOTP(secret).now()})
    assert resp.status_code == 403


# ─── /v1/mfa/setup, /v1/mfa/qrcode — auth/lookup guards only; see the
# module docstring's KNOWN BUG note for why the 200 path isn't tested.

def test_mfa_setup_requires_auth(client):
    assert client.post("/v1/mfa/setup").status_code == 401

# No "unknown user -> 404" test for mfa/setup: _get_authenticated_user
# does its own user_id -> row lookup (LocalAuthProvider.get_user), so
# authenticating at all already requires the row to exist — the route's
# own second lookup (same table, same id) can only 404 on a delete-
# between-the-two-lookups race, not from a token naming a user that
# never existed. Not worth forcing that race for one branch.


def test_mfa_qrcode_requires_auth(client):
    assert client.get("/v1/mfa/qrcode").status_code == 401


def test_mfa_qrcode_without_mfa_set_up_404s(client, fake_db):
    _seed_user(fake_db)
    token = _pyjwt.encode({"sub": "user@example.com"}, get_jwt_secret(), algorithm="HS256")
    resp = client.get("/v1/mfa/qrcode", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


# ─── /v1/forgot-password, /v1/reset-password ────────────────────────────

def test_forgot_password_success_sends_a_reset_email(client, fake_db, monkeypatch):
    _seed_user(fake_db)
    sent = {}
    monkeypatch.setattr(server_mod, "send_reset_password_email", lambda **kw: sent.update(kw))
    resp = client.post("/v1/forgot-password", json={"email": "user@example.com"})
    assert resp.status_code == 200
    assert sent["user_email"] == "user@example.com"
    assert fake_db.get_item("user", "user@example.com")["reset_token"]


def test_forgot_password_unknown_user_404s(client):
    resp = client.post("/v1/forgot-password", json={"email": "ghost@example.com"})
    assert resp.status_code == 404


def test_reset_password_success(client, fake_db):
    _seed_user(fake_db, reset_token="rt-1", reset_token_expiry=int(time.time()) + 3600)
    resp = client.post("/v1/reset-password", json={"resetToken": "rt-1", "newPassword": "new-password"})
    assert resp.status_code == 200
    row = fake_db.get_item("user", "user@example.com")
    assert row["reset_token"] is None
    from auth import password_util
    assert password_util.verify_password("new-password", row["password_hash"])


def test_reset_password_invalid_token_404s(client):
    resp = client.post("/v1/reset-password", json={"resetToken": "nope", "newPassword": "x"})
    assert resp.status_code == 404


def test_reset_password_expired_token_400s(client, fake_db):
    _seed_user(fake_db, reset_token="rt-1", reset_token_expiry=int(time.time()) - 10)
    resp = client.post("/v1/reset-password", json={"resetToken": "rt-1", "newPassword": "x"})
    assert resp.status_code == 400


# ─── /v1/accept-invite ───────────────────────────────────────────────
# accept_invite itself (auth.invite_flow) has its own behavior to test
# elsewhere; here it's mocked to isolate the route's own error mapping.

def test_accept_invite_success(client, monkeypatch):
    monkeypatch.setattr("auth.invite_flow.accept_invite", lambda *a, **kw: None)
    resp = client.post("/v1/accept-invite", json={"token": "t1", "newPassword": "x"})
    assert resp.status_code == 200


def test_accept_invite_invalid_token_maps_value_error_to_400(client, monkeypatch):
    def _boom(*a, **kw):
        raise ValueError("invite token expired")
    monkeypatch.setattr("auth.invite_flow.accept_invite", _boom)
    resp = client.post("/v1/accept-invite", json={"token": "t1", "newPassword": "x"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invite token expired"


# ─── /v1/refresh-token ───────────────────────────────────────────────

def test_refresh_token_success_rotates_the_token_and_updates_the_session(client, fake_db):
    _seed_user(fake_db)
    old_rt = str(uuid.uuid4())
    fake_db.insert_item("auth_session", "s1", {
        "id": "s1", "user_id": "user@example.com", "status": "active",
        "refresh_token_hash": hash_refresh_token(old_rt),
        "expires_at": int(time.time() * 1000) + 100_000, "last_used_at": int(time.time() * 1000),
    })
    resp = client.post("/v1/refresh-token", json={"refresh_token": old_rt})
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"] != old_rt
    session = fake_db.get_item("auth_session", "s1")
    assert session["refresh_token_hash"] == hash_refresh_token(body["refresh_token"])


def test_refresh_token_expired_session_is_rejected_and_marked_expired(client, fake_db):
    _seed_user(fake_db)
    rt = str(uuid.uuid4())
    fake_db.insert_item("auth_session", "s1", {
        "id": "s1", "user_id": "user@example.com", "status": "active",
        "refresh_token_hash": hash_refresh_token(rt), "expires_at": int(time.time() * 1000) - 1000,
    })
    resp = client.post("/v1/refresh-token", json={"refresh_token": rt})
    assert resp.status_code == 401
    assert fake_db.get_item("auth_session", "s1")["status"] == "expired"


def test_refresh_token_inactive_session_is_rejected(client, fake_db):
    rt = str(uuid.uuid4())
    fake_db.insert_item("auth_session", "s1", {
        "id": "s1", "user_id": "user@example.com", "status": "revoked",
        "refresh_token_hash": hash_refresh_token(rt),
    })
    resp = client.post("/v1/refresh-token", json={"refresh_token": rt})
    assert resp.status_code == 401


def test_refresh_token_unknown_token_with_no_oauth_configured_is_rejected(client):
    resp = client.post("/v1/refresh-token", json={"refresh_token": "never-issued"})
    assert resp.status_code == 401


# ─── /v1/logout ──────────────────────────────────────────────────────

def test_logout_deletes_active_sessions_and_clears_tokens(client, fake_db):
    _seed_user(fake_db, token="old-access", refresh_token="old-refresh")
    fake_db.insert_item("auth_session", "s1", {"id": "s1", "user_id": "user@example.com", "status": "active"})
    token = _pyjwt.encode({"sub": "user@example.com"}, get_jwt_secret(), algorithm="HS256")

    resp = client.post("/v1/logout", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert fake_db.get_item("auth_session", "s1") is None
    row = fake_db.get_item("user", "user@example.com")
    assert row["token"] is None
    assert row["refresh_token"] is None


def test_logout_without_an_authorization_header_is_rejected(client):
    assert client.post("/v1/logout").status_code == 401


def test_logout_with_an_unknown_user_is_rejected(client):
    token = _pyjwt.encode({"sub": "ghost@example.com"}, get_jwt_secret(), algorithm="HS256")
    resp = client.post("/v1/logout", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


# ─── /v1/register, /v1/verify/{token} ───────────────────────────────

def test_register_success_sends_a_verification_email(client, fake_db, monkeypatch):
    sent = {}
    monkeypatch.setattr(server_mod, "send_verification_email", lambda settings, reg: sent.update(email=reg.email))
    # Origin header: client_base_url falls back to self.settings.base_url
    # when Origin is absent, and base_url isn't a real field (see the
    # module docstring's KNOWN BUG) — supplying Origin short-circuits
    # that `or` chain before it gets there, same as a real browser POST
    # always would.
    resp = client.post(
        "/v1/register", json={"email": "new@example.com", "password": "x"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 200
    assert sent["email"] == "new@example.com"
    regs = fake_db.query_items("registration", {"email": "new@example.com"})
    assert len(regs) == 1
    assert regs[0]["password"] is None  # plaintext password never persisted
    assert regs[0]["password_hash"]


def test_register_missing_email_is_rejected(client):
    assert client.post("/v1/register", json={"email": "", "password": "x"}).status_code == 422


def test_register_invalid_email_format_is_rejected(client):
    assert client.post("/v1/register", json={"email": "not-an-email", "password": "x"}).status_code == 422


def test_register_already_registered_user_is_rejected(client, fake_db):
    _seed_user(fake_db, user_id="taken@example.com")
    resp = client.post(
        "/v1/register", json={"email": "taken@example.com", "password": "x"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 400


def test_register_pending_registration_is_rejected(client, fake_db, monkeypatch):
    monkeypatch.setattr(server_mod, "send_verification_email", lambda *a, **kw: None)
    headers = {"origin": "http://testserver"}
    client.post("/v1/register", json={"email": "pending@example.com", "password": "x"}, headers=headers)
    resp = client.post("/v1/register", json={"email": "pending@example.com", "password": "y"}, headers=headers)
    assert resp.status_code == 400


def test_verify_delegates_to_verify_and_register(client, monkeypatch):
    # response_model=ServiceResponseMessage fills in the rest of that
    # model's (mostly-optional) fields, so check the one that matters
    # rather than the full dict.
    monkeypatch.setattr(server_mod, "verify_and_register", lambda token: {"message": f"verified {token}"})
    resp = client.get("/v1/verify/abc123")
    assert resp.status_code == 200
    assert resp.json()["message"] == "verified abc123"


def test_verify_error_maps_to_500(client, monkeypatch):
    def _boom(token):
        raise ValueError("bad token")
    monkeypatch.setattr(server_mod, "verify_and_register", _boom)
    resp = client.get("/v1/verify/abc123")
    assert resp.status_code == 500


# ─── /v1/auth/resolve ────────────────────────────────────────────────

def test_resolve_auth_defaults_to_password_mode(client):
    resp = client.post("/v1/auth/resolve", json={"email": "anyone@example.com"})
    assert resp.status_code == 200
    assert resp.json() == {"mode": "password"}
