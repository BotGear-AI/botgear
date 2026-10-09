"""Shared HS256 JWT minting + verification for all AuthProvider implementations.

After OAuth callback or password login, the API server is the token authority —
the UI receives a JWT signed with ``JWT_SECRET_KEY`` that this same API server
can verify locally. Every ``AuthProvider`` (Local, Okta, Cognito, None) uses
the same logic; the differences between providers are only in *how identity is
verified*, not in token format.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

import jwt

logger = logging.getLogger(__name__)

# The single source of truth for the public dev fallback secret. Every consumer
# (the signer here, server.py's refresh-token hasher, botgear's subprocess
# auth, etc.) MUST resolve through get_jwt_secret() rather than re-typing this
# literal, so the fallback can never drift between call sites.
FALLBACK_JWT_SECRET = "fallback_dev_key"
_FALLBACK_JWT_SECRET = FALLBACK_JWT_SECRET  # backward-compatible alias
# Captured at import for backward-compatible imports of this name. The signer
# below reads the secret lazily (get_jwt_secret) so a server startup bridge that
# sets os.environ["JWT_SECRET_KEY"] from config takes effect even though this
# module was imported earlier. (#278)
JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", FALLBACK_JWT_SECRET)
JWT_EXPIRATION_MINUTES_DEFAULT = 60


def get_jwt_secret() -> str:
    """The effective HS256 secret, read fresh from the environment each call.

    This is THE resolver — every place that needs the JWT/HMAC signing secret
    should call it instead of doing ``os.environ.get("JWT_SECRET_KEY",
    "fallback_dev_key")`` inline, so the env-var name and fallback value stay
    normalized to a single definition.
    """
    return os.environ.get("JWT_SECRET_KEY") or JWT_SECRET_KEY


# Backward-compatible private alias (existing callers/tests import _jwt_secret).
_jwt_secret = get_jwt_secret


def is_weak_jwt_secret() -> bool:
    """True when the effective secret is unset or the public fallback."""
    secret = get_jwt_secret()
    return (not secret) or secret == FALLBACK_JWT_SECRET


def assert_jwt_secret_configured(require: Optional[bool] = None) -> None:
    """Fail-closed guard for deployments (#278).

    Always logs loudly when the public fallback secret is in effect (which allows
    token forgery). Whether that also *raises* is decided by ``require``:

      - ``require=True``  -> raise (fail-hard). Set by apps that must never run on
        the fallback (e.g. hyrule_api, via the ``require_jwt_secret`` DSL flag).
      - ``require=False`` -> warn only, keep running on the fallback. The default
        for localhost-first apps that must start with no env configured.
      - ``require=None``  -> defer to the ``REQUIRE_JWT_SECRET`` env var
        (backward-compatible behavior for callers that pass nothing).

    Call this once at server startup, after any config->env secret bridge.
    """
    if not is_weak_jwt_secret():
        return
    if require is None:
        require = os.environ.get("REQUIRE_JWT_SECRET", "").strip().lower() in ("1", "true", "yes")
    message = (
        "JWT_SECRET_KEY is unset or using the public fallback value; tokens can be "
        "forged. Set a strong secret (e.g. via <APP>_JWT_SECRET) before deploying."
    )
    if require:
        raise RuntimeError(message)
    logger.critical(message)


def resolve_jwt_expiration_minutes(ttl_minutes: Optional[int] = None) -> int:
    """Resolve the access-token TTL, in minutes.

    Precedence (highest first):
      1. ``JWT_EXPIRATION_MINUTES`` env var — local-test override that wins
         over everything. server.py always supplies an explicit ``ttl_minutes``
         value via the event-handler hook (the per-deployment knob), so without
         this env-var override there would be no way to short-circuit for
         testing without DB or config changes.
      2. Explicit ``ttl_minutes`` arg (set by ``server.py`` from
         ``on_get_jwt_access_token_ttl_minutes`` — sources from
         ``settings.jwt_access_token_ttl_minutes``).
      3. ``JWT_EXPIRATION_MINUTES_DEFAULT`` (60 minutes).
    """
    raw = os.environ.get("JWT_EXPIRATION_MINUTES")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            logger.warning(
                "Invalid JWT_EXPIRATION_MINUTES env var %r; falling back to other sources",
                raw,
            )
    if ttl_minutes is not None and ttl_minutes > 0:
        return ttl_minutes
    return JWT_EXPIRATION_MINUTES_DEFAULT


# avatar_url excluded because it can hold a data: URI (a client-side-encoded
# image, potentially several KB) -- embedding that in the JWT means every
# subsequent request re-sends it as part of the Authorization header, which
# has caused real requests to be rejected upstream (e.g. nginx's default
# large_client_header_buffers) for any user with an avatar set. The client
# already gets avatar_url from the login/user-fetch response body; it doesn't
# need to ride along in the token too.
_BLACKLIST = {"password_hash", "password_updated_at", "totp_secret", "reset_token", "reset_token_expiry", "password", "token", "avatar_url"}


def mint_jwt(user: dict, *, ttl_minutes: Optional[int] = None) -> str:
    """Mint an HS256 JWT for the given user dict.

    ``user`` must contain ``id`` and ``email``. The full dict (minus the blacklist
    of sensitive fields) is embedded under the ``user`` claim so endpoints can
    read role/group/tenant info without an extra DB roundtrip.
    """
    if not user:
        raise ValueError("mint_jwt: user cannot be empty")
    if not user.get("id"):
        raise ValueError("mint_jwt: user['id'] is required")
    if not user.get("email"):
        raise ValueError("mint_jwt: user['email'] is required")

    uid = user["id"].lower()
    user_payload = {k: v for k, v in user.items() if k not in _BLACKLIST}
    user_payload["id"] = uid
    user_payload["email"] = uid

    payload = {
        "sub": uid,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=resolve_jwt_expiration_minutes(ttl_minutes)),
        "jti": uuid.uuid4().hex,          # lets logout revoke this exact token
        "user": user_payload,
    }
    token = jwt.encode(payload, _jwt_secret(), algorithm="HS256")
    return token if isinstance(token, str) else token.decode("utf-8")


def _decode(token: str) -> Optional[dict]:
    """Verify an HS256 signature + expiry and return the raw claims, or None."""
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        logger.warning("JWT has expired")
    except jwt.InvalidTokenError:
        logger.warning("Invalid JWT")
    return None


def verify_jwt(token: str) -> Optional[dict]:
    """Verify an HS256 access JWT and return its payload, or None if invalid/expired.

    Single-purpose tokens (any ``purpose`` claim, e.g. mfa_pending) are rejected
    here, once, so no AuthProvider and no token->access path (get_user,
    refresh_token, logout) can accept one as a bearer credential.
    Read those with verify_purpose_token instead.
    """
    payload = _decode(token)
    if payload is not None and "purpose" in payload:
        logger.warning("Single-purpose JWT presented as an access token")
        return None
    if payload is not None and payload.get("jti") and is_revoked(payload["jti"]):
        logger.warning("Revoked JWT presented (signed out)")
        return None
    return payload


# --- access-token revocation ------------------------------------------------ #
# Logout revokes the presented access token by its ``jti`` until the token's
# own expiry, so a token held (or stolen) after sign-out stops working at once
# instead of living out its TTL. Revocations are kept in-process and, when the
# app installs one (set_revocation_store), in a store shared by every process.
# Tokens minted before ``jti`` existed can't be revoked; they simply expire.

class RevocationStore(Protocol):
    def revoke(self, jti: str, expires_at: int, subject: Optional[str]) -> None: ...
    def is_revoked(self, jti: str) -> bool: ...


_revoked: dict[str, int] = {}          # jti -> expiry (epoch seconds)
_revoked_lock = threading.Lock()
_store: Optional[RevocationStore] = None


def set_revocation_store(store: Optional[RevocationStore]) -> None:
    """Install the shared store (e.g. a database table) revocations go to."""
    global _store
    _store = store


def revoke_jwt(token: str) -> bool:
    """Revoke a valid access token until it expires. Returns False for a token
    that doesn't verify or carries no ``jti``."""
    if not token:
        return False
    payload = _decode(token)
    jti = (payload or {}).get("jti")
    if not jti:
        return False
    expires_at = int(payload.get("exp") or time.time())
    with _revoked_lock:
        now = time.time()
        for old in [k for k, exp in _revoked.items() if exp < now]:
            del _revoked[old]
        _revoked[jti] = expires_at
    if _store is not None:
        try:
            _store.revoke(jti, expires_at, payload.get("sub"))
        except Exception as e:  # the in-process revocation still holds
            logger.warning(f"Could not persist token revocation: {e}")
    return True


def is_revoked(jti: str) -> bool:
    with _revoked_lock:
        if jti in _revoked:
            return True
    if _store is not None:
        try:
            return _store.is_revoked(jti)
        except Exception as e:
            # Fail open on a store outage: refusing every request would take
            # the whole API down; the token still expires on schedule.
            logger.warning(f"Could not check token revocation: {e}")
    return False


# Single-purpose tokens carry a ``purpose`` claim and NO ``user`` claim.
# verify_jwt rejects any token with a purpose claim, so one can never be used as
# a bearer credential. Used for the short-lived ``mfa_pending`` token that binds
# /v1/mfa/login to a completed password step.
MFA_PENDING_PURPOSE = "mfa_pending"
MFA_PENDING_TTL_MINUTES = 5


def mint_purpose_token(subject: str, purpose: str, *, ttl_minutes: int, claims: Optional[dict] = None) -> str:
    """Mint a short-lived single-purpose HS256 token (no ``user`` claim).

    ``claims`` adds extra claims (e.g. a server-side session id); it cannot
    override ``sub``, ``purpose`` or ``exp``.
    """
    if not subject:
        raise ValueError("mint_purpose_token: subject is required")
    if not purpose:
        raise ValueError("mint_purpose_token: purpose is required")
    if not ttl_minutes or ttl_minutes <= 0:
        raise ValueError("mint_purpose_token: ttl_minutes must be positive")
    payload = {
        **(claims or {}),
        "sub": subject.lower(),
        "purpose": purpose,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes),
    }
    token = jwt.encode(payload, _jwt_secret(), algorithm="HS256")
    return token if isinstance(token, str) else token.decode("utf-8")


def verify_purpose_token(token: str, expected_purpose: str) -> Optional[dict]:
    """Return the claims of a valid single-purpose token, or None.

    Rejects expired/invalid tokens and any token whose ``purpose`` claim does not
    match ``expected_purpose`` (including full access tokens, which carry none)."""
    payload = _decode(token)
    if not payload or payload.get("purpose") != expected_purpose or not payload.get("sub"):
        return None
    return payload
