"""Contract tests for auth.invite_flow -- the single-use invite-accept path (#310).

FakeDB deliberately mimics the real database backends' default behavior of
dropping None-valued fields in update_item() unless include_nulls=True is
passed. Without that, a regression that drops include_nulls=True from
accept_invite() would leave invite_token/invite_token_expiry silently
un-cleared -- exactly the bug this module exists to fix -- and these tests
would still pass. Keeping that default enforced here is what actually
catches it.
"""
import time

import pytest

from auth.invite_flow import accept_invite


class FakeDB:
    def __init__(self, items=None):
        self.items = items if items is not None else {}

    def insert_item(self, table, item_id, data):
        self.items.setdefault(table, {})[item_id] = dict(data)
        return data

    def get_item(self, table, item_id):
        row = self.items.get(table, {}).get(item_id)
        return dict(row) if row is not None else None

    def query_items(self, table, query):
        rows = self.items.get(table, {}).values()
        return [
            dict(row) for row in rows
            if all(row.get(k) == v for k, v in query.items())
        ]

    def update_item(self, table, item_id, data, include_nulls=False):
        existing = self.items.setdefault(table, {}).setdefault(item_id, {})
        for k, v in data.items():
            if v is None and not include_nulls:
                continue
            existing[k] = v
        return dict(existing)


@pytest.fixture
def db():
    return FakeDB()


def _invited_user(*, token="tok-1", expiry=None, status="invited",
                   user_id="user@example.com", roles=None, tenant_id=None):
    return {
        "id": user_id,
        "email": user_id,
        "status": status,
        "invite_token": token,
        "invite_token_expiry": expiry if expiry is not None else int(time.time()) + 3600,
        "roles": roles if roles is not None else ["staff"],
        "tenant_id": tenant_id,
        "fullname": None,
    }


# ── validation ───────────────────────────────────────────────────────────


def test_db_missing_raises():
    with pytest.raises(ValueError, match="database not configured"):
        accept_invite(None, "tok-1", "new-password")


def test_missing_token_raises(db):
    with pytest.raises(ValueError, match="invite token is required"):
        accept_invite(db, "", "new-password")


def test_missing_password_raises(db):
    db.insert_item("user", "user@example.com", _invited_user())
    with pytest.raises(ValueError, match="a password is required"):
        accept_invite(db, "tok-1", "")


def test_unknown_token_raises(db):
    with pytest.raises(ValueError, match="Invalid or expired invite token"):
        accept_invite(db, "no-such-token", "new-password")


def test_non_pending_status_rejected(db):
    db.insert_item("user", "user@example.com", _invited_user(status="active"))
    with pytest.raises(ValueError, match="no longer pending"):
        accept_invite(db, "tok-1", "new-password")


def test_expired_token_rejected(db):
    db.insert_item("user", "user@example.com",
                    _invited_user(expiry=int(time.time()) - 10))
    with pytest.raises(ValueError, match="expired"):
        accept_invite(db, "tok-1", "new-password")


# ── success + null persistence (the include_nulls=True regression) ────────


def test_first_acceptance_persists_token_fields_as_null(db):
    db.insert_item("user", "user@example.com", _invited_user())

    result = accept_invite(db, "tok-1", "new-password")

    assert result["status"] == "accepted"
    stored = db.get_item("user", "user@example.com")
    assert stored["invite_token"] is None
    assert stored["invite_token_expiry"] is None
    assert stored["status"] == "active"
    assert stored["email_verified"] is True
    assert stored["password_hash"]


def test_acceptance_does_not_touch_roles_or_tenant(db):
    """Role/tenant come from the invite, never from the accept request."""
    db.insert_item("user", "user@example.com",
                    _invited_user(roles=["billing_admin"], tenant_id="tenant-9"))

    accept_invite(db, "tok-1", "new-password")

    stored = db.get_item("user", "user@example.com")
    assert stored["roles"] == ["billing_admin"]
    assert stored["tenant_id"] == "tenant-9"


def test_fullname_only_set_when_missing(db):
    db.insert_item("user", "user@example.com", _invited_user())
    accept_invite(db, "tok-1", "new-password", fullname="Jane Doe")
    assert db.get_item("user", "user@example.com")["fullname"] == "Jane Doe"


# ── replay rejection ────────────────────────────────────────────────────


def test_replay_after_accept_is_rejected(db):
    db.insert_item("user", "user@example.com", _invited_user())
    accept_invite(db, "tok-1", "first-password")

    with pytest.raises(ValueError, match="Invalid or expired invite token"):
        accept_invite(db, "tok-1", "second-password")

    # The replay must not have overwritten the first accepted password.
    stored = db.get_item("user", "user@example.com")
    first_hash = stored["password_hash"]
    assert first_hash
    with pytest.raises(ValueError):
        accept_invite(db, "tok-1", "third-password")
    assert db.get_item("user", "user@example.com")["password_hash"] == first_hash


def test_concurrent_replay_is_rejected(db):
    """Two requests race on the same token: both pass the initial lookup
    before either write commits. Simulated by having query_items report the
    token already consumed on its SECOND call within a single accept_invite
    -- the recheck immediately before the final write."""
    db.insert_item("user", "user@example.com", _invited_user())

    class RacingDB(FakeDB):
        def __init__(self, items):
            super().__init__(items)
            self.query_calls = 0

        def query_items(self, table, query):
            self.query_calls += 1
            if self.query_calls == 1:
                return super().query_items(table, query)
            # A concurrent winner already consumed the token between this
            # request's initial lookup and its write.
            return []

    racing_db = RacingDB(db.items)

    with pytest.raises(ValueError, match="already been accepted"):
        accept_invite(racing_db, "tok-1", "loser-password")

    # The loser must not have persisted its password over the winner's.
    stored = db.get_item("user", "user@example.com")
    assert stored.get("password_hash") is None
    assert stored["invite_token"] == "tok-1"  # untouched by the loser


# ── hook integration ────────────────────────────────────────────────────


def test_accept_hook_result_is_persisted(db):
    import types

    db.insert_item("user", "user@example.com", _invited_user())
    mod = types.ModuleType("fake.handlers")

    def on_invite_accepted(user, hook_db):
        user["welcomed"] = True
        return user

    mod.on_invite_accepted = on_invite_accepted

    accept_invite(db, "tok-1", "new-password", hook_module=mod)

    assert db.get_item("user", "user@example.com")["welcomed"] is True


def test_accept_hook_raising_falls_back_to_user(db, caplog):
    import types

    db.insert_item("user", "user@example.com", _invited_user())
    mod = types.ModuleType("fake.handlers")

    def on_invite_accepted(user, hook_db):
        raise RuntimeError("boom")

    mod.on_invite_accepted = on_invite_accepted

    with caplog.at_level("ERROR"):
        result = accept_invite(db, "tok-1", "new-password", hook_module=mod)

    assert result["status"] == "accepted"
    assert db.get_item("user", "user@example.com")["status"] == "active"
