# unmanaged
"""Smoke test for the sqlite database backend wired up in event_handlers.py.

Boots a real SqliteDatabase against every model botgear's on_startup()
ensures a table for, then round-trips a row through the same
insert/get/list_tables surface admin_db_api.py and the service layer use.
See TODO_SQLITE.md for why this backend exists (lowdb/TinyDB corruption on
an interrupted write).
"""
import sys
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest

from database.factory import create_database_client, get_database_client
from database.sqlite_database import SqliteDatabase
from models.database_sqlite_config import DatabaseSqliteConfig

from models.auth_session import AuthSession

from botgear.models.api_client import ApiClient
from botgear.models.config import Config as BotgearConfig
from botgear.models.registration import Registration
from botgear.models.service_config import ServiceConfig
from botgear.models.service_meta import ServiceMeta
from botgear.models.service_proxy import ServiceProxy
from botgear.models.service_request import ServiceRequest
from botgear.models.user import User
from botgear.models.workspace import Workspace

_MODELS = [
    AuthSession,
    Registration,
    User,
    ServiceMeta,
    ServiceProxy,
    ServiceConfig,
    ServiceRequest,
    Workspace,
    ApiClient,
    BotgearConfig,
]


@pytest.fixture
def sqlite_db(tmp_path):
    SqliteDatabase._instances.clear()
    db_path = tmp_path / "databases" / "botgear.db"
    name = f"test-{tmp_path.name}"
    create_database_client(DatabaseSqliteConfig(name=name, sqlite_path=str(db_path)))
    db = get_database_client(name)
    for model in _MODELS:
        db.ensure_table(model=model)
    yield db
    SqliteDatabase._instances.clear()


def test_ensure_table_creates_every_botgear_table(sqlite_db):
    tables = set(sqlite_db.list_tables())
    for model in _MODELS:
        table_name = model.__name__[0].lower() + "".join(
            f"_{c.lower()}" if c.isupper() else c for c in model.__name__[1:]
        )
        assert table_name in tables, f"missing table for {model.__name__}: have {tables}"


def test_service_proxy_round_trip(sqlite_db):
    """service_proxy is the table that took the whole runtime down under
    TinyDB on 2026-06-20 — verify the sqlite path round-trips a realistic
    row with nested config."""
    row = {
        "id": "brain-1",
        "name": "brain-1",
        "service_meta_id": "brain@1.0.0",
        "status": "running",
        "service_config": {"ollama_url": "http://localhost:11434", "model": "llama3"},
    }
    sqlite_db.insert_item("service_proxy", "brain-1", row)
    fetched = sqlite_db.get_item("service_proxy", "brain-1")
    assert fetched["service_meta_id"] == "brain@1.0.0"
    assert fetched["service_config"] == {"ollama_url": "http://localhost:11434", "model": "llama3"}

    all_items = sqlite_db.get_all_items("service_proxy")
    assert len(all_items) == 1


def test_config_default_seed_does_not_crash(sqlite_db):
    """Regression for the on_startup() config/default seed path
    (event_handlers.py): without ensure_table(model=BotgearConfig), the
    "config" table is never created and this sequence 500s on the very
    first boot under sqlite (get_item raises 'no such table: config')."""
    assert sqlite_db.get_item("config", "default") == {}
    settings = BotgearConfig()
    default_cfg = settings.model_dump()
    default_cfg["id"] = "default"
    sqlite_db.insert_item("config", "default", default_cfg)
    assert sqlite_db.get_item("config", "default")["database_type"] == "sqlite"


def test_duplicate_insert_raises_like_tinydb(sqlite_db):
    """first_user_routes.py's claim-first-user race guard depends on this."""
    sqlite_db.insert_item("user", "u1", {"id": "u1", "email": "a@example.com"})
    with pytest.raises(ValueError):
        sqlite_db.insert_item("user", "u1", {"id": "u1", "email": "b@example.com"})
