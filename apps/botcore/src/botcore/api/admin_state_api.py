# unmanaged
"""GET /v1/admin/state — structured snapshot of botcore state.

Backs the in-UI ``/admin/state`` page. Returns the same dict the CLI
``python -m botcore.tools.state --json`` produces. Admin-gated.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from auth import create_auth_dependencies
from config import create_app_settings
from fastapi import APIRouter, Depends

from botcore.models.config import Config
from botcore.tools.state import gather_state


settings, config_provider = create_app_settings("botcore", Config)
auth_deps = create_auth_dependencies(config_provider)
logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/admin/state", response_model=Dict[str, Any])
def get_state(_: Any = Depends(auth_deps.require_role(["Admin"]))) -> Dict[str, Any]:
    """Returns the same structure as the CLI tool. Cheap — milliseconds."""
    return gather_state()
