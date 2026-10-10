INACTIVE_USER_STATUSES = frozenset({"disabled", "locked", "invited"})


def is_inactive_user(user: dict) -> bool:
    """Whether a persisted account status blocks authentication."""
    status = (user or {}).get("status") or "active"
    return str(status).lower() in INACTIVE_USER_STATUSES
