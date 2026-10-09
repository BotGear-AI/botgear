# unmanaged
"""Explicit per-instance config read/save — the yml-aware counterpart to
the generic ``GET``/``PUT /v1/service-proxy/{id}`` CRUD routes.

    GET  /v1/service-proxy/{proxy_id}/config   -> {"config": {...}}
    POST /v1/service-proxy/{proxy_id}/config   {"config": {...}}
        -> {"ok": true}
    POST /v1/service-proxy/{proxy_id}/rename   {"new_name": "..."}
        -> rename report (see ``lifecycle.rename_service``)

Config now lives in the active config-set's ``<proxy_id>.yml`` (+ the live
Service), not the ``service_proxy`` DB row (see ``runtime/boot.py``'s
"stone 4b" notes) — a subprocess service reads ONLY that yml on start,
never the DB row's ``service_config`` (which is a stale, unused mirror
for subprocess types today). The generic CRUD PUT the Composer's
Configure-without-Start flow used to call writes just that DB row, so a
reconfigure through it was silently discarded on the instance's next
start.

Both routes exist together deliberately, not just POST: RJSF's
``ConfigWizard`` always submits the FULL config object (every schema
property gets a value, touched or not — see SchemaForm.tsx), not a
sparse patch. If the wizard seeded itself from the stale DB mirror,
POST's own onto-current-config merge can't tell "user left this
unchanged" from "user actually cleared it back to the mirror's stale
value" — both arrive as the same key with the same (stale) value, and
the stale value wins. So the seed has to be fresh too: ConfigWizard
fetches GET before rendering rather than trusting ``proxy.service_config``
straight off the already-cached proxy list, which would otherwise silently
revert any yml-only field (e.g. oakd's ``filters``/``mono_stream_camera``)
the DB mirror never had.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from auth import create_auth_dependencies
from config import create_app_settings
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from botgear.models.config import Config

settings, config_provider = create_app_settings("botgear", Config)
auth_deps = create_auth_dependencies(config_provider)
logger = logging.getLogger(__name__)

router = APIRouter()

_ROLES = ["Admin"]


class ProxyConfigResponse(BaseModel):
    config: Dict[str, Any]


class SaveProxyConfigRequest(BaseModel):
    config: Dict[str, Any] = Field(default_factory=dict)


class SaveProxyConfigResponse(BaseModel):
    ok: bool


def _require_proxy(db: Any, proxy_id: str) -> Dict[str, Any]:
    row = db.get_item("service_proxy", proxy_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"unknown proxy_id: {proxy_id}")
    return row


@router.get(
    "/service-proxy/{proxy_id}/config",
    response_model=ProxyConfigResponse,
)
def get_proxy_config(
    proxy_id: str,
    _: Any = Depends(auth_deps.require_role(_ROLES)),
) -> ProxyConfigResponse:
    from database.factory import get_database_client
    from botgear.runtime import lifecycle

    db = get_database_client()
    if db is None:
        raise HTTPException(status_code=500, detail="database client not initialised")
    _require_proxy(db, proxy_id)
    return ProxyConfigResponse(config=lifecycle.proxy_config(proxy_id))


@router.post(
    "/service-proxy/{proxy_id}/config",
    response_model=SaveProxyConfigResponse,
)
def save_proxy_config(
    proxy_id: str,
    body: SaveProxyConfigRequest,
    _: Any = Depends(auth_deps.require_role(_ROLES)),
) -> SaveProxyConfigResponse:
    from database.factory import get_database_client
    from botgear.runtime import lifecycle

    db = get_database_client()
    if db is None:
        raise HTTPException(status_code=500, detail="database client not initialised")
    row = _require_proxy(db, proxy_id)

    merged = {**lifecycle.proxy_config(proxy_id), **body.config}
    ok = lifecycle.save_proxy_config(proxy_id, merged)
    if ok and not row.get("configured"):
        row["configured"] = True
        db.update_item("service_proxy", proxy_id, row, include_nulls=True)
    logger.info("service-proxy-config: saved proxy=%s ok=%s keys=%s", proxy_id, ok, list(body.config.keys()))
    return SaveProxyConfigResponse(ok=ok)


# ─── rename ───────────────────────────────────────────────────────────


class RenameProxyRequest(BaseModel):
    new_name: str = Field(..., description="New service name (lowercase letters, digits, '-' and '_', starting with a letter)")


class RenameProxyResponse(BaseModel):
    old_id: str
    new_id: str
    proxy: Dict[str, Any]
    yml_renamed: bool
    registry_updated: bool
    references: Dict[str, int] = Field(default_factory=dict, description="proxy_id → number of references rewritten in its yml")
    reloaded: list[str] = Field(default_factory=list, description="Running services whose live config was reloaded with the new name")
    workspaces: list[str] = Field(default_factory=list)
    moved_dirs: list[str] = Field(default_factory=list)


@router.post(
    "/service-proxy/{proxy_id}/rename",
    response_model=RenameProxyResponse,
    summary="Rename a stopped service instance and every reference to it",
)
def rename_proxy(
    proxy_id: str,
    body: RenameProxyRequest,
    _: Any = Depends(auth_deps.require_role(_ROLES)),
) -> RenameProxyResponse:
    """Re-keys the instance's yml, registry slot, DB row, workspace layout
    and every other service's binding to it — see
    ``lifecycle.rename_service``. 409 when refused (running, singleton,
    name taken), 404 when unknown, 400 for an invalid name."""
    from botgear.runtime import lifecycle

    new_name = body.new_name.strip()
    try:
        lifecycle.validate_proxy_name(new_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        report = lifecycle.rename_service(proxy_id, new_name)
    except ValueError as exc:
        msg = str(exc)
        raise HTTPException(status_code=404 if "not found" in msg else 409, detail=msg)
    return RenameProxyResponse(**report)
