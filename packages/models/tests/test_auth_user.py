"""Regression test: User CRUD must never return password_hash/totp_secret.

`GET /v1/user-list` and `GET /v1/user/{id}` serialize AuthUser/User via the CRUD
factory's response_model. password_hash and totp_secret were plain fields, so both
secrets were served verbatim to any Admin-role caller (including tenant-confined
API keys). Only JWTs and the login response stripped them.

Fix: Field(..., exclude=True) on both fields -- enforced at the model layer, so
they are dropped from model_dump()/model_dump_json() regardless of which endpoint
serializes the model (mirrors the client_secret fix in PR #996). Direct attribute
access is unaffected, so the write paths (which re-add the secrets explicitly) and
authentication keep working.
"""

from __future__ import annotations

import json

from models.auth_user import AuthUser


PASSWORD_HASH = "scrypt$16384$8$1$deadbeef$cafebabe"
TOTP_SECRET = "totp-secret-placeholder-not-a-real-key"


def _user_with_secrets() -> AuthUser:
    return AuthUser(
        id="alice@example.com",
        email="alice@example.com",
        tenant_id="acme",
        roles=["Admin"],
        password_hash=PASSWORD_HASH,
        totp_secret=TOTP_SECRET,
        is_mfa_enabled=True,
    )


class TestAuthUserSecretExclusion:
    def test_model_dump_excludes_secrets(self):
        payload = _user_with_secrets().model_dump()
        assert "password_hash" not in payload
        assert "totp_secret" not in payload
        assert PASSWORD_HASH not in json.dumps(payload)
        assert TOTP_SECRET not in json.dumps(payload)

    def test_model_dump_json_excludes_secrets(self):
        raw = _user_with_secrets().model_dump_json()
        assert "password_hash" not in raw
        assert "totp_secret" not in raw
        assert PASSWORD_HASH not in raw
        assert TOTP_SECRET not in raw

    def test_model_dump_still_includes_non_secret_fields(self):
        """The exclusion must not be over-broad -- everything else still serializes."""
        payload = _user_with_secrets().model_dump()
        assert payload["id"] == "alice@example.com"
        assert payload["email"] == "alice@example.com"
        assert payload["tenant_id"] == "acme"
        assert payload["roles"] == ["Admin"]
        assert payload["is_mfa_enabled"] is True

    def test_direct_attribute_access_still_works(self):
        """exclude=True only affects serialization -- the write paths read the
        attribute directly and must keep getting the real value."""
        user = _user_with_secrets()
        assert user.password_hash == PASSWORD_HASH
        assert user.totp_secret == TOTP_SECRET

    def test_repr_and_str_exclude_secrets(self):
        """exclude=True only covers serialization. The other sink is logging —
        `logger.info(user)` / f"{user}" go through repr()/str(), so both fields
        also carry repr=False. Otherwise the scrypt hash and TOTP secret land
        in the log the first time someone logs the model."""
        user = _user_with_secrets()
        for rendered in (repr(user), str(user), f"{user}"):
            assert "password_hash" not in rendered
            assert "totp_secret" not in rendered
            assert PASSWORD_HASH not in rendered
            assert TOTP_SECRET not in rendered
        assert "alice@example.com" in repr(user)  # not over-broad

    def test_the_exact_production_pattern_that_leaked(self):
        """Reproduces the response path: model_dump() -> dict -> JSON, which is
        what the user CRUD routes serialize through."""
        user = _user_with_secrets()
        response_body = json.dumps(user.model_dump())
        assert PASSWORD_HASH not in response_body
        assert TOTP_SECRET not in response_body
