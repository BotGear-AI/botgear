# unmanaged
"""api_client CRUD + the X-API-Key validator the auth package calls into.

External integrations authenticate against botgear by sending their
shared secret in the ``X-API-Key`` request header. The packages/auth
``get_current_user_or_service`` dependency calls ``validate_api_client_secret``
(registered at startup in event_handlers.on_startup) which looks the secret
up in the api_client table and returns a synthetic principal whose ``roles``
drive the same role gates as the JWT-authenticated path.

NOTE on the DB call: the Postgres-backed reference apps
do this lookup with raw SQL (``db.query("select ... where secret = %s")``).
botgear runs the lowdb / TinyDB backend by default, whose ``query()`` raises
NotImplementedError — so the lookup here uses the backend-agnostic
``db.query_items(table, {"secret": secret})``, which both TinyDB and Postgres
implement. No secret cache: the reference TTLCache exists to spare a Postgres
connection pool; the in-process single-file TinyDB has no pool to protect.
"""
import logging
import secrets
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import Request

from database.factory import get_database_client
from botgear.models.api_client import ApiClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CRUD (called by the generated crud_router)
# ---------------------------------------------------------------------------

def create_api_client(item: ApiClient, user: dict, request: Request):
    logger.info("===============create_api_client called==============")
    item_id = item.id if getattr(item, "id", None) else str(uuid.uuid4())
    new_item = item.model_dump()
    new_item["id"] = item_id
    # Mint the secret server-side when the caller didn't supply one, so a
    # plain `POST /v1/api-client {"name": ...}` always yields a usable key.
    # token_urlsafe(32) ≈ 43 chars of URL-safe base64 (~256 bits).
    if not new_item.get("secret"):
        new_item["secret"] = secrets.token_urlsafe(32)
    if not new_item.get("created"):
        new_item["created"] = int(time.time() * 1000)
    db = get_database_client()
    if db:
        db.insert_item("api_client", item_id, new_item)
    logger.info(
        "api_client created: id=%s name=%r roles=%s",
        item_id, new_item.get("name"), new_item.get("roles"),
    )
    # Return the full row INCLUDING the secret — the create response is the
    # one place the caller sees the freshly-minted key (the list/get views
    # are what the UI re-displays later).
    return new_item


def get_all_api_client(user: dict, request: Request):
    logger.debug("===============get_all_api_client called==============")
    db = get_database_client()
    if db:
        return db.get_all_items("api_client")
    return []


def get_api_client(id: str, user: dict, request: Request):
    logger.debug("===============get_api_client called for id=%s==============", id)
    db = get_database_client()
    if db:
        return db.get_item("api_client", id)
    return None


def update_api_client(id: str, new_item: ApiClient, user: dict, request: Request):
    logger.info("===============update_api_client called for id=%s==============", id)
    db = get_database_client()
    if not db:
        return None
    # 404 explicitly when the row doesn't exist — otherwise an update of a
    # missing row serializes a default-populated model back as if it upserted.
    if not db.get_item("api_client", id):
        return None
    # PATCH-style partial update: write ONLY the fields the caller actually
    # sent (model_fields_set), so omitted fields are preserved. This is
    # load-bearing for `roles`: its model default is [] (NOT None), so a plain
    # model_dump() would emit roles=[] on every request and — since the
    # backend's include_nulls=False filter only drops None — a rename-only PUT
    # (`{"name": "x"}`) would silently wipe an Admin key down to no roles.
    # exclude_unset keeps an omitted roles untouched while still honoring an
    # EXPLICIT roles=[] in the body as a genuine "remove all roles".
    db.update_item("api_client", id, new_item.model_dump(exclude_unset=True))
    return db.get_item("api_client", id)


def delete_api_client(id: str, user: dict, request: Request):
    logger.info("===============delete_api_client called for id=%s==============", id)
    db = get_database_client()
    if not db:
        return None
    item = db.get_item("api_client", id)
    if not item:
        logger.warning("ApiClient with id %s not found", id)
        return None
    db.delete_item("api_client", id)
    return item


# ---------------------------------------------------------------------------
# Internal — wired into auth_deps.set_shared_secret_validator at startup.
# NOT a route (api_client.__methods__ deliberately excludes it).
# ---------------------------------------------------------------------------

def validate_api_client_secret(secret: str) -> Optional[Dict[str, Any]]:
    """Resolve an X-API-Key secret to an authenticated principal.

    Return contract (drives the upstream HTTP status in auth_util.py):
      * dict  -> a row matched; the caller treats it as the authenticated
        principal (auth_util stamps ``auth_type="shared_secret"``) and its
        ``roles`` flow into the same role gates as a JWT user.
      * None  -> lookup ran but nothing matched -> 401 "Invalid API key".
      * raise -> the lookup itself failed (DB unavailable) -> 500. Operational
        failures must raise, not return None, so a bad key never masks an
        outage.
    """
    if not secret:
        return None
    db = get_database_client()
    if not db:
        raise RuntimeError("Database client not available")

    rows = db.query_items(table_name="api_client", criteria={"secret": secret})
    if not rows:
        logger.warning("No api_client matched the presented X-API-Key")
        return None

    # Build the principal from the row but never propagate the secret itself
    # into the request context / logs.
    principal = dict(rows[0])
    principal.pop("secret", None)
    principal.setdefault("roles", [])
    principal["auth_type"] = "shared_secret"
    return principal
