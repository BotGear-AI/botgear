import logging
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from auth import jwt_util
from auth.jwt_util import (
    FALLBACK_JWT_SECRET,
    assert_jwt_secret_configured,
    get_jwt_secret,
    is_weak_jwt_secret,
    mint_jwt,
    resolve_jwt_expiration_minutes,
    verify_jwt,
)


def _user(**overrides):
    user = {"id": "user@example.com", "email": "user@example.com", "roles": ["Admin"]}
    user.update(overrides)
    return user


@pytest.fixture
def weak_secret(monkeypatch):
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    monkeypatch.setattr(jwt_util, "JWT_SECRET_KEY", FALLBACK_JWT_SECRET)


@pytest.fixture
def strong_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "a-strong-test-secret")
    return "a-strong-test-secret"


def test_get_jwt_secret_reads_environment_each_call(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "first")
    assert get_jwt_secret() == "first"
    monkeypatch.setenv("JWT_SECRET_KEY", "second")
    assert get_jwt_secret() == "second"


def test_is_weak_for_fallback_and_empty(weak_secret, monkeypatch):
    assert is_weak_jwt_secret() is True
    monkeypatch.setattr(jwt_util, "JWT_SECRET_KEY", "")
    assert is_weak_jwt_secret() is True


def test_strong_secret_passes_guard_even_when_required(strong_secret):
    assert is_weak_jwt_secret() is False
    assert_jwt_secret_configured(require=True)


def test_guard_raises_when_required(weak_secret):
    with pytest.raises(RuntimeError, match="forged"):
        assert_jwt_secret_configured(require=True)


def test_guard_only_logs_when_not_required(weak_secret, monkeypatch, caplog):
    monkeypatch.setenv("REQUIRE_JWT_SECRET", "true")  # explicit arg wins over env
    with caplog.at_level(logging.CRITICAL, logger="auth.jwt_util"):
        assert_jwt_secret_configured(require=False)
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


@pytest.mark.parametrize("value", ["1", " Yes ", "TRUE"])
def test_guard_defers_to_env_when_require_is_none(weak_secret, monkeypatch, value):
    monkeypatch.setenv("REQUIRE_JWT_SECRET", value)
    with pytest.raises(RuntimeError):
        assert_jwt_secret_configured()


def test_guard_env_unset_or_falsey_does_not_raise(weak_secret, monkeypatch, caplog):
    monkeypatch.setenv("REQUIRE_JWT_SECRET", "no")
    with caplog.at_level(logging.CRITICAL, logger="auth.jwt_util"):
        assert_jwt_secret_configured()
    assert "JWT_SECRET_KEY is unset" in caplog.text


def test_non_positive_env_ttl_falls_through_to_arg(monkeypatch):
    monkeypatch.setenv("JWT_EXPIRATION_MINUTES", "0")
    assert resolve_jwt_expiration_minutes(ttl_minutes=5) == 5


@pytest.mark.parametrize(
    "user, message",
    [({}, "cannot be empty"), ({"email": "a@b.c"}, r"user\['id'\]"), ({"id": "a@b.c"}, r"user\['email'\]")],
)
def test_mint_jwt_validates_user(user, message):
    with pytest.raises(ValueError, match=message):
        mint_jwt(user)


def test_mint_jwt_strips_blacklist_and_normalizes_id(strong_secret, monkeypatch):
    monkeypatch.delenv("JWT_EXPIRATION_MINUTES", raising=False)
    token = mint_jwt({
        "id": "Alice@Example.com", "email": "other@example.com", "roles": ["Admin"],
        "password_hash": "h", "totp_secret": "t", "token": "k", "reset_token": "r",
    }, ttl_minutes=2)

    payload = jwt.decode(token, strong_secret, algorithms=["HS256"])
    assert payload["sub"] == "alice@example.com"
    assert payload["user"]["id"] == "alice@example.com"
    assert payload["user"]["email"] == "alice@example.com"
    assert payload["user"]["roles"] == ["Admin"]
    for key in ("password_hash", "totp_secret", "token", "reset_token"):
        assert key not in payload["user"]
    remaining = payload["exp"] - datetime.now(timezone.utc).timestamp()
    assert 100 <= remaining <= 125
    assert verify_jwt(token)["sub"] == "alice@example.com"


def test_verify_jwt_expired_returns_none(strong_secret, caplog):
    token = jwt.encode(
        {"sub": "x", "exp": datetime.now(timezone.utc) - timedelta(seconds=5)},
        strong_secret, algorithm="HS256",
    )
    with caplog.at_level(logging.WARNING, logger="auth.jwt_util"):
        assert verify_jwt(token) is None
    assert "expired" in caplog.text


def test_verify_jwt_wrong_signature_returns_none(strong_secret, caplog):
    token = jwt.encode({"sub": "x"}, "some-other-secret", algorithm="HS256")
    with caplog.at_level(logging.WARNING, logger="auth.jwt_util"):
        assert verify_jwt(token) is None
    assert "Invalid JWT" in caplog.text


def test_mint_jwt_embeds_user_claims():
    token = mint_jwt(_user())
    payload = verify_jwt(token)
    assert payload["user"]["roles"] == ["Admin"]


def test_mint_jwt_excludes_password_hash():
    token = mint_jwt(_user(password_hash="secret-hash"))
    payload = verify_jwt(token)
    assert "password_hash" not in payload["user"]


def test_mint_jwt_excludes_avatar_url():
    """avatar_url can be a several-KB data: URI (client-side image upload).

    Embedding it in the JWT means it gets re-sent as part of the
    Authorization header on every subsequent request -- large enough to trip
    a reverse proxy's header-size limit (nginx's default
    large_client_header_buffers) and get every request rejected with a 400
    before it ever reaches the app. The client already has avatar_url from
    the login/user-fetch response body, so it must not also ride in the
    token.
    """
    big_avatar = "data:image/jpeg;base64," + ("A" * 20000)
    token = mint_jwt(_user(avatar_url=big_avatar))
    payload = verify_jwt(token)
    assert "avatar_url" not in payload["user"]
    assert len(token) < 4000


# --- single-purpose tokens --------------------------------------------------- #

@pytest.mark.parametrize("subject, purpose", [("", "mfa_pending"), ("alice@example.com", "")])
def test_mint_purpose_token_validates_args(subject, purpose):
    with pytest.raises(ValueError):
        jwt_util.mint_purpose_token(subject, purpose, ttl_minutes=5)


def test_mint_purpose_token_requires_positive_ttl():
    with pytest.raises(ValueError):
        jwt_util.mint_purpose_token("alice@example.com", jwt_util.MFA_PENDING_PURPOSE, ttl_minutes=0)


def test_mint_purpose_token_round_trip_normalizes_subject():
    token = jwt_util.mint_purpose_token(
        "Alice@Example.com", jwt_util.MFA_PENDING_PURPOSE, ttl_minutes=5,
        claims={"sid": "sess_1", "sub": "mallory", "purpose": "other"},
    )
    # No user claim; carries the purpose; subject lowercased; extra claims can't
    # override the reserved ones.
    payload = jwt_util.verify_purpose_token(token, jwt_util.MFA_PENDING_PURPOSE)
    assert payload["purpose"] == jwt_util.MFA_PENDING_PURPOSE
    assert "user" not in payload
    assert payload["sub"] == "alice@example.com"
    assert payload["sid"] == "sess_1"


def test_verify_jwt_rejects_purpose_tokens():
    # Enforced once here so no AuthProvider can accept one as a bearer.
    token = jwt_util.mint_purpose_token("alice@example.com", jwt_util.MFA_PENDING_PURPOSE, ttl_minutes=5)
    assert verify_jwt(token) is None


def test_verify_purpose_token_rejects_wrong_purpose():
    token = jwt_util.mint_purpose_token("alice@example.com", jwt_util.MFA_PENDING_PURPOSE, ttl_minutes=5)
    assert jwt_util.verify_purpose_token(token, "something_else") is None


def test_verify_purpose_token_rejects_access_token():
    # A full access token has no purpose claim and must not satisfy a purpose check.
    access = mint_jwt({"id": "alice@example.com", "email": "alice@example.com"})
    assert jwt_util.verify_purpose_token(access, jwt_util.MFA_PENDING_PURPOSE) is None


def test_verify_purpose_token_rejects_expired():
    token = jwt.encode(
        {"sub": "x", "purpose": jwt_util.MFA_PENDING_PURPOSE,
         "exp": datetime.now(timezone.utc) - timedelta(seconds=5)},
        get_jwt_secret(), algorithm="HS256",
    )
    assert jwt_util.verify_purpose_token(token, jwt_util.MFA_PENDING_PURPOSE) is None


def test_verify_purpose_token_rejects_garbage():
    assert jwt_util.verify_purpose_token("not-a-jwt", jwt_util.MFA_PENDING_PURPOSE) is None



# --- access-token revocation ------------------------------------------------ #

def test_logout_revocation_rejects_the_token_until_it_expires(monkeypatch):
    from auth import jwt_util
    monkeypatch.setattr(jwt_util, "_revoked", {})
    token = jwt_util.mint_jwt({"id": "u@example.com", "email": "u@example.com"})
    other = jwt_util.mint_jwt({"id": "u@example.com", "email": "u@example.com"})
    assert jwt_util.verify_jwt(token)["jti"] != jwt_util.verify_jwt(other)["jti"]
    assert jwt_util.revoke_jwt(token) is True
    assert jwt_util.verify_jwt(token) is None
    assert jwt_util.verify_jwt(other) is not None           # only that token
    assert jwt_util.revoke_jwt("not-a-token") is False


def test_revocations_go_to_and_come_from_the_shared_store(monkeypatch):
    from auth import jwt_util

    class Store:
        def __init__(self):
            self.rows = {}

        def revoke(self, jti, expires_at, subject):
            self.rows[jti] = (expires_at, subject)

        def is_revoked(self, jti):
            return jti in self.rows

    store = Store()
    monkeypatch.setattr(jwt_util, "_revoked", {})
    monkeypatch.setattr(jwt_util, "_store", store)
    token = jwt_util.mint_jwt({"id": "u@example.com", "email": "u@example.com"})
    jwt_util.revoke_jwt(token)
    jti = jwt_util._decode(token)["jti"]
    assert store.rows[jti][1] == "u@example.com"
    jwt_util._revoked.clear()                                # another process: only the store knows
    assert jwt_util.verify_jwt(token) is None


def test_a_failing_store_does_not_take_auth_down(monkeypatch):
    from auth import jwt_util

    class Broken:
        def revoke(self, *a):
            raise RuntimeError("db down")

        def is_revoked(self, jti):
            raise RuntimeError("db down")

    monkeypatch.setattr(jwt_util, "_revoked", {})
    monkeypatch.setattr(jwt_util, "_store", Broken())
    token = jwt_util.mint_jwt({"id": "u@example.com", "email": "u@example.com"})
    assert jwt_util.verify_jwt(token) is not None
    assert jwt_util.revoke_jwt(token) is True                # still revoked in-process
    assert jwt_util.verify_jwt(token) is None
