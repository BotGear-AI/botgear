import hashlib

from .jwt_util import _jwt_secret


def hash_refresh_token(token: str, secret: str = None) -> str:
    """
    Hash a refresh token with a server-side secret pepper.

    Uses SHA-256 over "<secret>:<token>" so that a leaked DB row cannot be
    replayed without also knowing the server secret.

    Returns a string of the form "sha256:<hex>".
    """
    if secret is None:
        # Single source of truth with the JWT signer (#303): reads the same
        # secret that server startup bridges from config into JWT_SECRET_KEY, so
        # the pepper no longer degrades to its own separate hardcoded fallback.
        secret = _jwt_secret()
    combined = f"{secret}:{token}"
    digest = hashlib.sha256(combined.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
