"""Regression coverage for a real bug (found + fixed 2026-09-15): /v1/logout's
handler called self.auth._verify_jwt(token) unconditionally, but that is a
LocalAuthProvider-only internal -- OAuthAuthProvider has no such method, so
under OAuth (hyrule_api's default auth_type) every logout call raised
AttributeError, got silently swallowed by a bare except, and the endpoint
always 401'd before ever reaching session cleanup.

Fixed by resolving the caller through the provider-agnostic AuthProvider.
get_user(token) instead (the same method /v1/oauth/logout already used
successfully). These tests pin down the two facts the fix depends on:
OAuthAuthProvider genuinely has no _verify_jwt (so nothing should ever call
it), and get_user() correctly resolves an id/email from a real token.
"""

from __future__ import annotations

from auth.jwt_util import mint_jwt
from auth.oauth_auth import OAuthAuthProvider


def _provider() -> OAuthAuthProvider:
    return OAuthAuthProvider(config={"auth_type": "oauth"})


class TestOAuthAuthProviderHasNoVerifyJwt:
    def test_no_verify_jwt_method(self):
        """Documents the actual root cause: this method simply does not
        exist here. Nothing in the codebase should call
        self.auth._verify_jwt() without first checking auth_type -- the
        provider-agnostic get_user()/logout() interface methods exist
        precisely so callers never need to."""
        assert not hasattr(_provider(), "_verify_jwt")


class TestOAuthAuthProviderGetUser:
    def test_get_user_resolves_id_and_email_from_a_real_token(self):
        # mint_jwt normalizes id/email to the same lowercased uid (the "sub"
        # claim) -- id and email are the same identity string in this
        # system, not independent fields.
        token = mint_jwt({"id": "Person@Example.com", "email": "Person@Example.com"})
        user = _provider().get_user(token)
        assert user is not None
        assert user["id"] == "person@example.com"
        assert user["email"] == "person@example.com"

    def test_get_user_returns_none_for_garbage_token(self):
        assert _provider().get_user("not-a-real-jwt") is None

    def test_get_user_rejects_single_purpose_token(self):
        # An mfa_pending token carries a purpose claim and must never resolve to
        # a principal, or it could be replayed as a bearer credential.
        from auth.jwt_util import mint_purpose_token, MFA_PENDING_PURPOSE

        token = mint_purpose_token("person@example.com", MFA_PENDING_PURPOSE, ttl_minutes=5)
        assert _provider().get_user(token) is None

    def test_get_user_falls_back_to_sub_claim_for_id_when_user_claim_thin(self):
        """mint_jwt always embeds id/email under the "user" claim, but
        get_user() also has a sub-claim fallback (used by /v1/logout's
        `(identity or {}).get('id') or (identity or {}).get('email')`
        pattern) -- exercise that path directly against a hand-built
        payload rather than only ever going through mint_jwt."""
        import jwt as pyjwt
        from auth.jwt_util import _jwt_secret

        token = pyjwt.encode({"sub": "someone@example.com"}, _jwt_secret(), algorithm="HS256")
        user = _provider().get_user(token)
        assert user is not None
        assert user["id"] == "someone@example.com"
        assert user["email"] == "someone@example.com"
