# unmanaged
"""Self-update API.

- ``GET  /v1/update/check``    (Admin): is a newer framework release available
  for this platform? Read-only detector behind the "Check for updates" UI.
- ``POST /v1/update/framework`` (Admin): apply it — download → verify → back up
  var/ → swap → restart into the new version (docs/TODO_UPGRADE_FEATURE.md §5).
  Progress streams on the retained bus topic ``/system/update/progress``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from auth import create_auth_dependencies
from config import create_app_settings, get_settings
from fastapi import APIRouter, Body, Depends, FastAPI
from pydantic import BaseModel, Field

from botcore.models.config import Config
from botcore.runtime import update_apply, updater

logger = logging.getLogger(__name__)

settings, config_provider = create_app_settings("botcore", Config)
auth_deps = create_auth_dependencies(config_provider)
_ROLES = ["Admin"]

_DEFAULT_UPDATE_REPO = "BotGear-AI/botgear"
_PROGRESS_TOPIC = "/system/update/progress"

# One framework update at a time, process-wide.
_update_lock = asyncio.Lock()


class UpdateCheckResponse(BaseModel):
    current: str = Field(..., description="The running framework build version")
    latest: Optional[str] = Field(None, description="Latest release tag on the update repo (null if unreachable)")
    update_available: bool = Field(False, description="True iff a newer release exists WITH an asset for this platform")
    reason: str = Field("", description="Human-readable explanation of the verdict")
    platform: str = Field(..., description="This host's bundle platform tag, e.g. linux-aarch64")
    repo: str = Field(..., description="GitHub owner/name polled for releases")
    asset_url: Optional[str] = Field(None, description="Download URL of the platform tarball on the latest release")
    sha256_url: Optional[str] = Field(None, description="Download URL of the tarball's .sha256 sidecar")
    published_at: Optional[str] = Field(None, description="ISO-8601 publish time of the latest release")
    notes: Optional[str] = Field(None, description="Release notes / changelog body")
    checked_at: str = Field(..., description="ISO-8601 time this check ran")
    error: Optional[str] = Field(None, description="Set when the update source could not be reached")


class UpdateFrameworkRequest(BaseModel):
    target_version: Optional[str] = Field(None, description="Version to update to; defaults to the latest release. MVP supports the latest only.")


class UpdateFrameworkResponse(BaseModel):
    accepted: bool = Field(..., description="True if the update started (watch /system/update/progress)")
    target: Optional[str] = Field(None, description="The version being applied, if accepted")
    message: str = Field(..., description="Why it was accepted or refused")


def _current_version() -> str:
    # Lazy import — server.py mounts this module, so a top-level import would
    # be circular. At request time server is fully loaded.
    try:
        from botcore.server import version as _v
        return str((_v or {}).get("version") or "0.0.0")
    except Exception:  # noqa: BLE001
        return "0.0.0"


def _repo() -> str:
    return getattr(get_settings(), "update_repo", None) or _DEFAULT_UPDATE_REPO


def _publish_progress(phase: str, pct: Optional[int], message: str) -> None:
    """Push an update phase onto the retained bus topic so the UI's progress
    view sees it (and a late subscriber catches the latest phase)."""
    try:
        from botcore.runtime.bus import get_bus
        get_bus().publish_sync(
            _PROGRESS_TOPIC,
            {"phase": phase, "pct": pct, "message": message,
             "ts": datetime.now(timezone.utc).isoformat()},
            retained=True,
        )
    except Exception:  # noqa: BLE001 — progress is best-effort
        logger.exception("update: progress publish failed")


def register_update_routes(app: FastAPI, get_db_provider=None) -> None:
    """Mount the self-update routes under /v1."""
    router = APIRouter()

    @router.get("/update/check", response_model=UpdateCheckResponse, tags=["Update"])
    async def update_check(_: Any = Depends(auth_deps.require_role(_ROLES))) -> UpdateCheckResponse:
        current = _current_version()
        ptag = updater.platform_tag()
        repo = _repo()
        now = datetime.now(timezone.utc).isoformat()
        try:
            rel = await updater.fetch_latest_release(repo)
        except Exception as exc:  # noqa: BLE001 — graceful "couldn't check", never a 500
            logger.warning("update.check: could not reach %s: %s", repo, exc)
            return UpdateCheckResponse(
                current=current, latest=None, update_available=False,
                reason="could not reach the update source", platform=ptag, repo=repo,
                checked_at=now, error=f"{type(exc).__name__}: {exc}",
            )
        available, reason = updater.decide(current, rel.version)
        tarball, sha = rel.platform_asset(ptag)
        if available and not tarball:
            available = False
            reason = f"a newer release ({rel.version}) exists but has no {ptag} build"
        return UpdateCheckResponse(
            current=current, latest=rel.version, update_available=available, reason=reason,
            platform=ptag, repo=repo, asset_url=tarball, sha256_url=sha,
            published_at=rel.published_at, notes=rel.notes, checked_at=now,
        )

    @router.post("/update/framework", response_model=UpdateFrameworkResponse, tags=["Update"])
    async def update_framework(
        body: UpdateFrameworkRequest = Body(default_factory=UpdateFrameworkRequest),
        _: Any = Depends(auth_deps.require_role(_ROLES)),
    ) -> UpdateFrameworkResponse:
        if not update_apply.is_frozen():
            return UpdateFrameworkResponse(accepted=False, target=None,
                                           message="self-update is only available in a packaged build")
        if _update_lock.locked():
            return UpdateFrameworkResponse(accepted=False, target=None,
                                           message="an update is already in progress")

        current = _current_version()
        ptag = updater.platform_tag()
        try:
            rel = await updater.fetch_latest_release(_repo())
        except Exception as exc:  # noqa: BLE001
            return UpdateFrameworkResponse(accepted=False, target=None,
                                           message=f"could not reach the update source: {exc}")

        if body.target_version and body.target_version not in (rel.version, rel.version.removeprefix("v")):
            return UpdateFrameworkResponse(accepted=False, target=rel.version,
                                           message="this MVP can only update to the latest release")

        available, reason = updater.decide(current, rel.version)
        if not available:
            return UpdateFrameworkResponse(accepted=False, target=rel.version, message=reason)
        tarball, sha = rel.platform_asset(ptag)
        if not tarball or not sha:
            return UpdateFrameworkResponse(accepted=False, target=rel.version,
                                           message=f"the latest release has no {ptag} build")

        # ``run_update`` ends in execv on success, so it runs detached. The
        # version it stamps/extracts to is the BARE tag (build.sh's VERSION,
        # no leading 'v').
        bare = rel.version.removeprefix("v")

        async def _run() -> None:
            async with _update_lock:
                try:
                    await update_apply.run_update(bare, tarball, sha, publish=_publish_progress)
                except Exception:  # noqa: BLE001 — run_update already published 'failed'
                    logger.exception("update: framework update task ended in error")

        asyncio.create_task(_run())
        _publish_progress("queued", 0, f"update to {rel.version} accepted")
        return UpdateFrameworkResponse(accepted=True, target=rel.version, message=f"updating to {rel.version}")

    app.include_router(router, prefix="/v1", tags=["Update"])
    logger.info("update: registered /v1/update/check + /v1/update/framework")
