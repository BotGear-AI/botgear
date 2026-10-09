"""Regression test for a real, live secret leak (found + fixed 2026-09-15).

/v1/oauth-config (templates/tpl/server.py.j2, generated into 14 apps
including hyrule_api) built its response with
`provider_config.model_dump()` then `JSONResponse(payload)` -- a raw dict,
never filtered through FastAPI's response_model. AuthOauthProviderConfig.
client_secret had no field-level exclusion, so the real OAuth client secret
was served verbatim to callers, unauthenticated ones too (oauth_config_public
defaults True everywhere except continuous_mfa). Live since 2026-04-03
(commit 4b39c9066, templates/tpl/server.py.j2's first version).

Fix: Field(..., exclude=True) on client_secret -- enforced at the model
layer, so it's dropped from model_dump()/model_dump_json() regardless of
which endpoint (this one, or any future one) serializes the model, not just
by the "must not go to client" comment that was already there and already
ignored. Direct attribute access (provider_config.client_secret) is
unaffected, so the legitimate server-side OAuth token-exchange call (which
needs the real secret) keeps working.
"""

from __future__ import annotations

import json

from models.auth_oauth_provider_config import AuthOauthProviderConfig


SECRET = "super-secret-oauth-client-secret-value"


def _configured_provider() -> AuthOauthProviderConfig:
    return AuthOauthProviderConfig(
        provider_id="okta",
        issuer="https://example.okta.com",
        client_id="my-public-client-id",
        client_secret=SECRET,
        redirect_uri="https://app.example.com/oauth2/callback",
        authorize_url="https://example.okta.com/oauth2/authorize",
    )


class TestAuthOauthProviderConfigClientSecretExclusion:
    def test_model_dump_excludes_client_secret(self):
        payload = _configured_provider().model_dump()
        assert "client_secret" not in payload
        assert SECRET not in json.dumps(payload)

    def test_model_dump_json_excludes_client_secret(self):
        raw = _configured_provider().model_dump_json()
        assert "client_secret" not in raw
        assert SECRET not in raw

    def test_model_dump_still_includes_non_secret_fields(self):
        """The fix must not be an over-broad exclusion that breaks the
        endpoint's actual purpose -- everything else still needs to reach
        the client."""
        payload = _configured_provider().model_dump()
        assert payload["client_id"] == "my-public-client-id"
        assert payload["issuer"] == "https://example.okta.com"
        assert payload["redirect_uri"] == "https://app.example.com/oauth2/callback"
        assert payload["authorize_url"] == "https://example.okta.com/oauth2/authorize"

    def test_direct_attribute_access_still_works(self):
        """exclude=True only affects serialization -- the legitimate
        server-side OAuth token-exchange call reads provider_config.
        client_secret directly and must keep getting the real value."""
        provider = _configured_provider()
        assert provider.client_secret == SECRET

    def test_the_exact_production_pattern_that_leaked(self):
        """Reproduces the actual code path: model_dump() -> dict used to
        build a JSONResponse. This is what /v1/oauth-config's handler does
        (server.py.j2); confirms the fix holds through that exact shape."""
        provider = _configured_provider()
        payload = provider.model_dump()
        payload["auth_url"] = provider.authorize_url or provider.issuer
        response_body = json.dumps(payload)
        assert "client_secret" not in response_body
        assert SECRET not in response_body
