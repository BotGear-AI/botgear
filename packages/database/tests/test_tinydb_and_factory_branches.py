"""TinyDB guard/schema branches and the database factory dispatch tables."""
import logging

import pytest
from pydantic import BaseModel
from tinydb.table import Document

from database import factory
from database.tinydb import TinyDBDatabase
from models.database_dynamodb_config import DatabaseDynamodbConfig
from models.database_filesystem_config import DatabaseFilesystemConfig
from models.database_mongodb_config import DatabaseMongodbConfig
from models.database_postgres_config import DatabasePostgresConfig
from models.database_s3_config import DatabaseS3Config
from models.database_sqlite_config import DatabaseSqliteConfig
from models.database_tinydb_config import DatabaseTinydbConfig


@pytest.fixture
def tdb(tmp_path):
    return TinyDBDatabase(DatabaseTinydbConfig(data_dir=str(tmp_path / "tiny")))


def test_insert_item_refuses_duplicate_key(tdb):
    tdb.insert_item("t", "k", {"v": "original"})
    # TinyDB itself also raises on a duplicate doc_id; match the adapter's own
    # message so this pins the explicit guard, not the library's fallback.
    with pytest.raises(ValueError, match="already exists in table 't'"):
        tdb.insert_item("t", "k", {"v": "clobber"})
    assert tdb.get_item("t", "k")["v"] == "original"


def test_update_item_null_handling(tdb):
    tdb.insert_item("t", "k", {"v": 1, "w": 2})
    assert tdb.update_item("t", "k", {"id": "x", "v": None}) == {"id": "k", "v": 1, "w": 2}
    assert tdb.update_item("t", "k", {"v": None}, include_nulls=True)["v"] is None


def test_query_items_without_criteria_returns_all(tdb):
    tdb.insert_item("t", "a", {"v": 1})
    tdb.insert_item("t", "b", {"v": 2})
    assert {r["id"] for r in tdb.query_items("t", {})} == {"a", "b"}


def test_copy_table_uses_uuid_fallback_and_skips_keyless_rows(tdb):
    source = tdb._get_db("src")
    source.insert(Document({"id": "with-id", "v": 1}, doc_id="with-id"))
    source.insert(Document({"uuid": "with-uuid", "v": 2}, doc_id="with-uuid"))
    source.insert(Document({"v": 3}, doc_id="keyless"))

    tdb.copy_table("src", "dst")

    assert tdb.get_item("dst", "with-id")["v"] == 1
    assert tdb.get_item("dst", "with-uuid")["v"] == 2
    assert tdb.get_item("dst", "keyless") == {}
    assert len(tdb.get_all_items("dst")) == 2


def test_insert_columns(tdb):
    with pytest.raises(ValueError, match="must match"):
        tdb.insert_columns("t", ["a", "b"], [1])
    tdb.insert_columns("t", ["a", "b"], [1, "x"])
    assert tdb.get_all_items("t") == [{"a": 1, "b": "x"}]


class AuditEntry(BaseModel):
    id: str


def test_ensure_table_and_list_tables(tdb, tmp_path):
    tdb.ensure_table(AuditEntry)
    tdb.ensure_table(AuditEntry, table_name="custom")
    base = tmp_path / "tiny"
    (base / "notes.txt").write_text("not a table")
    (base / "folder.json").mkdir()

    assert (base / "audit_entry.json").is_file()
    assert tdb.list_tables() == ["audit_entry", "custom"]


def test_list_tables_when_base_dir_is_gone(tdb, tmp_path):
    (tmp_path / "tiny").rmdir()
    assert tdb.list_tables() == []


# ── factory ───────────────────────────────────────────────────────────────


def _recorder(label):
    class Recorder:
        backend = label

        def __init__(self, config):
            self.config = config

    return Recorder


BACKENDS = [
    ("DynamoDBDatabase", DatabaseDynamodbConfig),
    ("TinyDBDatabase", DatabaseTinydbConfig),
    ("S3Database", DatabaseS3Config),
    ("FilesystemDatabase", DatabaseFilesystemConfig),
    ("SqliteDatabase", DatabaseSqliteConfig),
    ("MongoDBDatabase", DatabaseMongodbConfig),
    ("PostgresDatabase", DatabasePostgresConfig),
]


@pytest.fixture
def fake_backends(monkeypatch):
    monkeypatch.setattr(factory, "_clients", {})
    for class_name, _ in BACKENDS:
        monkeypatch.setattr(factory, class_name, _recorder(class_name))


@pytest.mark.parametrize("class_name, config_cls", BACKENDS)
def test_create_database_client_dispatches_on_config_type(fake_backends, class_name, config_cls):
    cfg = config_cls.model_construct(name=f"client-{class_name}")
    factory.create_database_client(cfg)
    client = factory.get_database_client(f"client-{class_name}")
    assert client.backend == class_name
    assert client.config is cfg


def test_create_database_client_requires_name(fake_backends):
    with pytest.raises(ValueError, match="name"):
        factory.create_database_client(object())


def test_create_database_client_never_replaces_existing(fake_backends, caplog):
    first = DatabaseSqliteConfig.model_construct(name="shared")
    factory.create_database_client(first)
    with caplog.at_level(logging.ERROR):
        factory.create_database_client(DatabasePostgresConfig.model_construct(name="shared"))
    assert factory.get_database_client("shared").config is first
    assert "already exists" in caplog.text


def test_create_database_client_rejects_unknown_config(fake_backends):
    class OtherConfig:
        name = "other"

    with pytest.raises(ValueError, match="Unsupported database config type"):
        factory.create_database_client(OtherConfig())
    assert factory.get_database_client("other") is None


@pytest.mark.parametrize(
    "class_name, config_cls",
    [("SqliteDatabase", DatabaseSqliteConfig), ("MongoDBDatabase", DatabaseMongodbConfig),
     ("PostgresDatabase", DatabasePostgresConfig)],
)
def test_get_database_dispatches_typed_configs(fake_backends, class_name, config_cls):
    cfg = config_cls.model_construct()
    db = factory.get_database(lambda: cfg)
    assert db.backend == class_name and db.config is cfg


def test_get_database_legacy_dict_config(fake_backends):
    cfg = {"database_type": "SQLite"}
    assert factory.get_database(lambda: cfg).backend == "SqliteDatabase"
    with pytest.raises(ValueError, match="Unsupported database type: redis"):
        factory.get_database(lambda: {"database_type": "redis"})
