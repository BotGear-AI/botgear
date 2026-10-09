# unmanaged
"""Per-client tool policy for the MCP gateway.

Each authenticated caller gets a ``ToolPolicy`` (agent_surface), the same
shape and evaluation as a brain workflow's ``allowed_tools.yaml``:

    <data_dir>/mcp/profiles/<client>.yaml     one client (API-client name, else id; users: email)
    <data_dir>/mcp/profiles/default.yaml      everyone without their own file
    DEFAULT_PROFILE (below)                   when neither file exists

Blocked always wins; ``default_policy: allow`` always carries the shared
hardware excludes (movement, arm, system, video, motors) — see
``botgear/agent_surface/excludes.py``. HALT is enforced separately, on
every call, whatever the profile says.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

from botgear.agent_surface import ToolPolicy, load_policy_file, policy_from_dict

logger = logging.getLogger(__name__)

# The built-in profile: what general_assistant may do, minus config writes.
# Anything live and not blocked is callable; on top of the shared hardware
# excludes it blocks
#   * brain's self-control surface — an external agent must not start other
#     workflows, cancel or approve runs, or touch model credentials;
#   * persisted service configuration — set/save/reload_config on any
#     service is an operator action, not something a remote agent should do
#     by default (give a specific client its own profile to allow it);
#   * the agent service's run control — an external agent must not launch
#     (or cancel) further agent runs through botgear by default.
DEFAULT_PROFILE: Dict[str, Any] = {
    "default_policy": "allow",
    "blocked": [
        {"topic": "/brain/*/control", "actions": [
            "start_workflow", "cancel", "approve", "clear_backend", "set_backend",
            "set_active_backend", "save_workflow_preferences", "save_run_configuration",
            "delete_run_configuration", "set_default_configuration", "clear_runs", "test_backend",
        ]},
        {"topic": "/*/*/control", "actions": ["set_config", "save_config", "reload_config"]},
        {"topic": "/agent/*/control", "actions": ["start_run", "cancel_run"]},
    ],
}

_SAFE = re.compile(r"[^A-Za-z0-9_.@-]+")


def principal_key(principal: Optional[Dict[str, Any]]) -> str:
    """Stable, filename-safe identity for a caller: an API client's name
    (else id), a user's email (else id/username)."""
    p = principal or {}
    if p.get("auth_type") == "shared_secret":
        raw = p.get("name") or p.get("id") or "api-client"
    else:
        raw = p.get("email") or p.get("username") or p.get("id") or "user"
    return _SAFE.sub("_", str(raw)).strip("._") or "unknown"


def profiles_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "mcp" / "profiles"


def load_profile(data_dir: Path, principal: Optional[Dict[str, Any]]) -> ToolPolicy:
    """The caller's own profile file, else ``default.yaml``, else
    DEFAULT_PROFILE. A malformed file fails closed (default-deny, nothing
    callable) rather than falling back to something more permissive."""
    pdir = profiles_dir(data_dir)
    for path in (pdir / f"{principal_key(principal)}.yaml", pdir / "default.yaml"):
        if path.is_file():
            try:
                return load_policy_file(path)
            except Exception:  # noqa: BLE001
                logger.exception("mcp_gateway: profile %s is invalid — denying everything", path)
                return ToolPolicy()
    return policy_from_dict(DEFAULT_PROFILE)
