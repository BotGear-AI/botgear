"""Branch coverage for LocalAuthProvider: bootstrap modes, guards, and token paths."""
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest

from auth import local_auth
from auth.jwt_util import get_jwt_secret, verify_jwt
from auth.local_auth import USERS_TABLE, LocalAuthProvider

ADMIN_ID = "admin@cloudseeder.ai"


class FakeUserDB:
    def __init__(self, rows=None):
        self.rows = {k: dict(v) for k, v in (rows or {}).items()}
        self.inserts = []
        self.updates = []

    def get_all_items(self, table):
        assert table == USERS_TABLE
        return [dict(r) for r in self.rows.values()]

    def get_item(self, table, key):
        row = self.rows.get(key)
        return dict(row) if row else {}

    def insert_item(self, table, key, item):
        self.inserts.append(key)
        self.rows[key] = dict(item)
        return item

    def update_item(self, table, key, updates):
        self.updates.append((key, dict(updates)))
        self.rows.setdefault(key, {}).update(updates)
        return self.rows[key]


@pytest.fixture(autouse=True)
def strong_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-local-auth")


@pytest.fixture
def make_provider(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def _make(db, config=None):
        monkeypatch.setattr(LocalAuthProvider, "_instance", None)
        monkeypatch.setattr(local_auth, "get_database_client", lambda: db)
        return LocalAuthProvider(config if config is not None else {"data_dir": str(tmp_path / "data")})

    return _make


def _token_without_sub():
    return jwt.encode(
        {"exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        get_jwt_secret(),
        algorithm="HS256",
    )


# ── bootstrap ─────────────────────────────────────────────────────────────


def test_admin_seed_creates_admin_with_password_in_data_dir(make_provider, tmp_path):
    db = FakeUserDB()
    provider = make_provider(db)

    password_file = tmp_path / "data" / "admin_password.txt"
    assert db.inserts == [ADMIN_ID]
    assert db.rows[ADMIN_ID]["roles"] == ["Admin"]
    assert password_file.is_file()
    assert provider.authenticate(ADMIN_ID.upper(), password_file.read_text()) is not None


def test_admin_seed_reuses_existing_password_file(make_provider, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "admin_password.txt").write_text("  known-pw\n")
    provider = make_provider(FakeUserDB())
    assert provider.authenticate(ADMIN_ID, "known-pw") is not None


def test_admin_seed_without_data_dir_writes_to_cwd(make_provider, tmp_path):
    db = FakeUserDB()
    make_provider(db, config={})
    assert (tmp_path / "admin_password.txt").is_file()
    assert db.inserts == [ADMIN_ID]


def test_non_dict_config_uses_admin_seed_and_cwd(make_provider, tmp_path):
    db = FakeUserDB()
    make_provider(db, config=SimpleNamespace(data_dir=str(tmp_path / "ignored")))
    assert db.inserts == [ADMIN_ID]
    assert (tmp_path / "admin_password.txt").is_file()
    assert not (tmp_path / "ignored").exists()


def test_admin_seed_skipped_when_users_exist(make_provider, tmp_path):
    db = FakeUserDB({"someone@example.com": {"id": "someone@example.com"}})
    make_provider(db)
    assert db.inserts == []
    assert not (tmp_path / "data" / "admin_password.txt").exists()


def test_first_user_claim_skips_admin_seed(make_provider, tmp_path):
    db = FakeUserDB()
    make_provider(db, config={"auth_bootstrap": "first_user_claim", "data_dir": str(tmp_path / "data")})
    assert db.inserts == []
    assert not (tmp_path / "data").exists()


def test_no_database_configured_logs_warning(make_provider, caplog):
    with caplog.at_level("WARNING", logger="auth.local_auth"):
        provider = make_provider(None)
    assert provider.database is None
    assert "does not appear to be a database configured" in caplog.text


def test_singleton_init_runs_once(make_provider):
    db = FakeUserDB()
    first = make_provider(db)
    db.rows.clear()
    second = LocalAuthProvider({"data_dir": "elsewhere"})
    assert second is first
    assert db.inserts == [ADMIN_ID]  # no re-seed on the second construction


# ── register_user ─────────────────────────────────────────────────────────


def test_register_user_with_plain_password(make_provider):
    db = FakeUserDB({"x@example.com": {"id": "x@example.com"}})
    provider = make_provider(db)

    result = provider.register_user("New@Example.com", password="pw-123")

    assert result == {"username": "new@example.com", "message": "User registered"}
    assert db.rows["new@example.com"]["password_hash"].startswith("scrypt$")
    assert provider.authenticate("new@example.com", "pw-123") is not None


def test_register_user_requires_password_or_hash(make_provider):
    provider = make_provider(FakeUserDB({"x": {"id": "x"}}))
    with pytest.raises(ValueError, match="required"):
        provider.register_user("nobody@example.com")


def test_register_user_rejects_password_and_hash_together(make_provider):
    provider = make_provider(FakeUserDB({"x": {"id": "x"}}))
    with pytest.raises(ValueError, match="same time"):
        provider.register_user("both@example.com", password="pw", password_hash="scrypt$...")


# ── authenticate ──────────────────────────────────────────────────────────


def test_authenticate_rejects_empty_user_id(make_provider):
    provider = make_provider(FakeUserDB({"x": {"id": "x"}}))
    with pytest.raises(ValueError, match="cannot be empty"):
        provider.authenticate("", "pw")


def test_authenticate_unknown_user_returns_none_without_writes(make_provider):
    db = FakeUserDB({"x": {"id": "x"}})
    provider = make_provider(db)
    assert provider.authenticate("ghost@example.com", "pw") is None
    assert db.updates == []


def test_authenticate_upgrades_legacy_sha256_hash(make_provider):
    legacy = hashlib.sha256(b"old-pw").hexdigest()
    db = FakeUserDB({"legacy@example.com": {"id": "legacy@example.com", "email": "legacy@example.com", "password_hash": legacy}})
    provider = make_provider(db)

    token = provider.authenticate("legacy@example.com", "old-pw")

    row = db.rows["legacy@example.com"]
    assert verify_jwt(token)["sub"] == "legacy@example.com"
    assert row["password_hash"].startswith("scrypt$")
    assert provider.verify_password("old-pw", row["password_hash"])
    assert isinstance(row["password_updated_at"], int)
    assert row["login_count"] == 1
    assert isinstance(row["last_login"], int)


def test_authenticate_wrong_password_records_failed_attempt(make_provider):
    db = FakeUserDB({"u@example.com": {"id": "u@example.com", "password_hash": hashlib.sha256(b"right").hexdigest()}})
    provider = make_provider(db)

    assert provider.authenticate("u@example.com", "wrong") is None
    assert "last_unsuccessful_login" in db.rows["u@example.com"]
    assert "last_login" not in db.rows["u@example.com"]


# ── tokens ────────────────────────────────────────────────────────────────


def test_generate_jwt_rejects_empty_user_id(make_provider):
    provider = make_provider(FakeUserDB({"x": {"id": "x"}}))
    with pytest.raises(ValueError, match="cannot be empty"):
        provider._generate_jwt("")


def test_generate_jwt_strips_credential_material(make_provider):
    db = FakeUserDB({
        "secret@example.com": {
            "id": "secret@example.com", "email": "secret@example.com", "roles": ["User"],
            "password_hash": "h", "totp_secret": "t", "reset_token": "r",
        }
    })
    provider = make_provider(db)

    claims = verify_jwt(provider._generate_jwt("SECRET@example.com"))["user"]

    assert claims["roles"] == ["User"]
    for key in ("password_hash", "totp_secret", "reset_token"):
        assert key not in claims


def test_generate_jwt_synthesizes_id_for_unknown_user(make_provider):
    provider = make_provider(FakeUserDB({"x": {"id": "x"}}))
    claims = verify_jwt(provider._generate_jwt("Ghost@Example.com"))
    assert claims["sub"] == "ghost@example.com"
    assert claims["user"]["email"] == "ghost@example.com"


def test_get_user_strips_secrets_and_handles_missing_users(make_provider):
    db = FakeUserDB({"u@example.com": {"id": "u@example.com", "email": "u@example.com", "password_hash": "h", "token": "t"}})
    provider = make_provider(db)
    token = provider.generate_token({"id": "u@example.com", "email": "u@example.com"})

    user = provider.get_user(token)
    assert user["id"] == "u@example.com"
    assert "password_hash" not in user and "token" not in user

    del db.rows["u@example.com"]
    assert provider.get_user(token) is None
    assert provider.get_user("not-a-jwt") is None


@pytest.mark.parametrize("method", ["get_user", "refresh_token", "logout"])
def test_token_without_sub_is_rejected(make_provider, method):
    provider = make_provider(FakeUserDB({"x": {"id": "x"}}))
    with pytest.raises(ValueError, match="cannot be empty"):
        getattr(provider, method)(_token_without_sub())


def test_refresh_token_invalid_or_unknown_user(make_provider, caplog):
    db = FakeUserDB({"x": {"id": "x"}})
    provider = make_provider(db)
    orphan = provider.generate_token({"id": "gone@example.com", "email": "gone@example.com"})

    with caplog.at_level("WARNING", logger="auth.local_auth"):
        assert provider.refresh_token("garbage") is None
        assert provider.refresh_token(orphan) is None
    assert caplog.text.count("Invalid refresh token provided.") == 2
    assert db.updates == []


def test_logout_invalid_token_returns_false(make_provider):
    db = FakeUserDB({"x": {"id": "x"}})
    provider = make_provider(db)
    orphan = provider.generate_token({"id": "gone@example.com", "email": "gone@example.com"})
    assert provider.logout("garbage") is False
    assert provider.logout(orphan) is False
    assert db.updates == []
