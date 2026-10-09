"""Tests for auth.session_manager: create / validate-and-rotate / revoke."""
from unittest.mock import MagicMock

import pytest

from auth import session_manager
from auth.session_manager import create_session, revoke_all_sessions, validate_and_rotate
from auth.session_policy import REFRESH_TOKEN_TTL_MS, SESSION_MAX_LIFETIME_MS
from auth.token_util import hash_refresh_token

NOW_MS = 1_700_000_000_000


class FakeSessionDB:
    """In-memory stand-in for the DatabaseAdapter surface session_manager uses."""

    def __init__(self):
        self.rows = {}
        self.updates = []
        self.fail_delete_ids = set()

    def insert_item(self, table, item_id, item):
        assert table == "auth_session"
        if item_id in self.rows:
            raise ValueError("duplicate")
        self.rows[item_id] = dict(item)

    def update_item(self, table, item_id, item):
        assert table == "auth_session"
        self.updates.append((item_id, dict(item)))
        self.rows[item_id] = dict(item)

    def query_items(self, table, filters):
        assert table == "auth_session"
        return [
            dict(row) for row in self.rows.values()
            if all(row.get(k) == v for k, v in filters.items())
        ]

    def delete_item(self, table, item_id):
        assert table == "auth_session"
        if item_id in self.fail_delete_ids:
            raise RuntimeError("delete failed")
        del self.rows[item_id]


@pytest.fixture
def frozen_time(monkeypatch):
    monkeypatch.setattr(session_manager.time, "time", lambda: NOW_MS / 1000)


def _seed(db, refresh_token="tok", **overrides):
    row = {
        "id": "sess_1",
        "user_id": "u1",
        "refresh_token_hash": hash_refresh_token(refresh_token),
        "status": "active",
        "created": NOW_MS - 1000,
        "expires_at": NOW_MS + 1000,
        "last_used_at": NOW_MS - 1000,
    }
    row.update(overrides)
    db.rows[row["id"]] = row
    return row


def test_create_session_persists_hash_not_raw_token(frozen_time):
    db = FakeSessionDB()
    token, session_id = create_session(
        db, {"id": "u1", "tenant_id": "t1"}, user_agent="ua", ip_address="10.0.0.1"
    )

    row = db.rows[session_id]
    assert session_id.startswith("sess_") and len(session_id) == len("sess_") + 16
    assert row["refresh_token_hash"] == hash_refresh_token(token)
    assert token not in row.values()
    assert row["user_id"] == "u1"
    assert row["tenant_id"] == "t1"
    assert row["status"] == "active"
    assert row["created"] == NOW_MS
    assert row["last_used_at"] == NOW_MS
    assert row["expires_at"] == NOW_MS + REFRESH_TOKEN_TTL_MS
    assert row["user_agent"] == "ua"
    assert row["ip_address"] == "10.0.0.1"
    assert db.updates == []


def test_create_session_falls_back_to_update_when_insert_fails(frozen_time):
    db = MagicMock()
    db.insert_item.side_effect = RuntimeError("already exists")

    token, session_id = create_session(db, {"id": "u1"})

    db.update_item.assert_called_once()
    table, updated_id, row = db.update_item.call_args.args
    assert (table, updated_id) == ("auth_session", session_id)
    assert row["refresh_token_hash"] == hash_refresh_token(token)
    assert row["tenant_id"] is None


def test_validate_and_rotate_rotates_token(frozen_time):
    db = FakeSessionDB()
    _seed(db, refresh_token="old")

    session, new_token = validate_and_rotate(db, "old")

    assert new_token != "old"
    stored = db.rows["sess_1"]
    assert stored["refresh_token_hash"] == hash_refresh_token(new_token)
    assert stored["last_used_at"] == NOW_MS
    assert session["id"] == "sess_1"
    # The old token must no longer validate.
    with pytest.raises(ValueError, match="Invalid refresh token"):
        validate_and_rotate(db, "old")


def test_validate_and_rotate_rejects_unknown_token(frozen_time):
    db = FakeSessionDB()
    _seed(db, refresh_token="real")
    with pytest.raises(ValueError, match="Invalid refresh token"):
        validate_and_rotate(db, "forged")
    assert db.updates == []


def test_validate_and_rotate_rejects_inactive_session(frozen_time):
    db = FakeSessionDB()
    _seed(db, status="revoked")
    with pytest.raises(ValueError, match="not active"):
        validate_and_rotate(db, "tok")
    assert db.updates == []


def test_validate_and_rotate_expires_past_rolling_window(frozen_time):
    db = FakeSessionDB()
    _seed(db, expires_at=NOW_MS - 1)
    with pytest.raises(ValueError, match="Refresh token expired"):
        validate_and_rotate(db, "tok")
    assert db.rows["sess_1"]["status"] == "expired"


def test_validate_and_rotate_enforces_hard_max_lifetime(frozen_time):
    db = FakeSessionDB()
    # Rolling window still open, but the session is older than the hard max.
    _seed(db, created=NOW_MS - SESSION_MAX_LIFETIME_MS - 1, expires_at=NOW_MS + 10_000)
    with pytest.raises(ValueError, match="max lifetime"):
        validate_and_rotate(db, "tok")
    assert db.rows["sess_1"]["status"] == "expired"


def test_validate_and_rotate_allows_session_at_exact_max_lifetime(frozen_time):
    db = FakeSessionDB()
    _seed(db, created=NOW_MS - SESSION_MAX_LIFETIME_MS)
    _, new_token = validate_and_rotate(db, "tok")
    assert db.rows["sess_1"]["refresh_token_hash"] == hash_refresh_token(new_token)


def test_revoke_all_sessions_deletes_only_active_rows_for_user():
    db = FakeSessionDB()
    _seed(db, id="a", user_id="u1", refresh_token="t1")
    _seed(db, id="b", user_id="u1", refresh_token="t2")
    _seed(db, id="c", user_id="u1", refresh_token="t3", status="expired")
    _seed(db, id="d", user_id="u2", refresh_token="t4")

    assert revoke_all_sessions(db, "u1") == 2
    assert set(db.rows) == {"c", "d"}


def test_revoke_all_sessions_counts_only_successful_deletes(caplog):
    db = FakeSessionDB()
    _seed(db, id="a", refresh_token="t1")
    _seed(db, id="b", refresh_token="t2")
    db.fail_delete_ids = {"a"}

    with caplog.at_level("ERROR", logger="auth.session_manager"):
        assert revoke_all_sessions(db, "u1") == 1

    assert "a" in db.rows and "b" not in db.rows
    assert "Failed to delete session a" in caplog.text
