"""Canonical invite-accept flow (#310).

Staff onboarding is invite-by-email: an admin creates a *pending* user row
(``status='invited'`` with roles/tenant/facilities set at invite time — the
role comes FROM the invite), and an emailed single-use token lets the invitee
set their OWN password and activate the account. This mirrors the password
reset flow, and lives in the auth package so every app can reuse it.

App-specific concerns — who may invite, which roles, the email body, the tenant
— stay in the app (its user service + the optional ``on_invite_accepted`` hook).
This module only knows the ``user`` table and the token contract:

- ``invite_token``        : single-use UUID stored on the pending user row
- ``invite_token_expiry`` : epoch **seconds** after which the token is dead

Public surface
--------------
- ``generate_invite_token(ttl_days=7) -> (token, expiry_epoch_seconds)``
- ``accept_invite(db, token, new_password, *, fullname=None, hook_module=None) -> dict``
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional, Tuple

from auth.password_util import hash_password

logger = logging.getLogger(__name__)

DEFAULT_INVITE_TTL_DAYS = 7


def generate_invite_token(ttl_days: int = DEFAULT_INVITE_TTL_DAYS) -> Tuple[str, int]:
    """Return ``(token, expiry_epoch_seconds)`` for a new invite."""
    token = str(uuid.uuid4())
    expiry = int(time.time()) + int(ttl_days) * 24 * 3600
    return token, expiry


def accept_invite(
    db,
    token: str,
    new_password: str,
    *,
    fullname: Optional[str] = None,
    hook_module: Any = None,
) -> dict:
    """Redeem a pending invite: validate the token, set the invitee's password,
    and activate the account.

    Returns one of:
        {"status": "accepted", "user": <dict>, "message": "..."}
    Raises ``ValueError`` on an invalid/expired/already-accepted token or a
    missing password (the caller maps these to 400/404).

    The token is single-use: it is cleared on success. Roles/tenant/facilities
    are NOT touched here — they were set at invite time (role from invite).
    """
    if db is None:
        raise ValueError("invite database not configured")
    if not token:
        raise ValueError("invite token is required")
    if not new_password:
        raise ValueError("a password is required to accept the invite")

    # Positional query_items(table, query) — the Postgres-safe convention.
    matches = db.query_items("user", {"invite_token": token}) or []
    if not matches:
        raise ValueError("Invalid or expired invite token")
    user = matches[0]

    # Only a genuinely pending invite may be accepted. A row that is already
    # active (or disabled/locked) must not be re-credentialed via this path.
    if user.get("status") != "invited":
        raise ValueError("This invitation is no longer pending")

    expiry = user.get("invite_token_expiry")
    if not expiry or int(time.time()) > int(expiry):
        raise ValueError("Invite token has expired")

    now_ms = int(time.time() * 1000)
    user["password_hash"] = hash_password(new_password)  # scrypt
    user["password_updated_at"] = now_ms
    user["status"] = "active"
    user["email_verified"] = True
    user["invite_token"] = None
    user["invite_token_expiry"] = None
    user["modified"] = now_ms
    if fullname and not user.get("fullname"):
        user["fullname"] = fullname

    user = _invoke_accept_hook(hook_module, user, db)

    # Not a true compare-and-swap: two requests racing on the same token can
    # both pass the query at the top of this function before either write
    # commits, so both would still succeed without this. Re-checking
    # immediately before the write narrows that window to a single
    # query-then-update pair instead of the whole function body (same
    # approach as /v1/reset-password in server.py). Closing it fully needs a
    # conditional/atomic update primitive in the shared DatabaseAdapter
    # (packages/database), which every backend would need to implement --
    # out of scope here.
    still_pending = db.query_items("user", {"invite_token": token}) or []
    if not still_pending or still_pending[0].get("id") != user["id"]:
        raise ValueError("This invitation has already been accepted")

    # include_nulls=True: update_item() silently drops None-valued fields by
    # default, which without this flag means invite_token/invite_token_expiry
    # above never actually reach the database -- the single-use invite token
    # stays valid and the link can be replayed until its own TTL expires.
    db.update_item("user", user["id"], user, include_nulls=True)
    logger.info("accept_invite: user %s activated (roles=%s)", user.get("id"), user.get("roles"))
    return {
        "status": "accepted",
        "user": user,
        "message": "Invitation accepted — you can now sign in",
    }


def _invoke_accept_hook(hook_module: Any, user: dict, db) -> dict:
    """Call ``hook_module.on_invite_accepted(user, db) -> dict`` if defined.
    Falls back to the user dict on every failure mode (missing/raises/wrong
    type) so a broken hook never breaks account activation."""
    if hook_module is None:
        return user
    hook = getattr(hook_module, "on_invite_accepted", None)
    if not callable(hook):
        return user
    try:
        result = hook(user, db)
    except Exception:
        logger.exception("accept_invite: on_invite_accepted hook raised; using user as-is")
        return user
    return result if isinstance(result, dict) else user
