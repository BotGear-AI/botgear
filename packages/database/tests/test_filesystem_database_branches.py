"""Error arms, insert_columns and ensure_table for FilesystemDatabase."""
import json
import logging
import os

import pytest
from pydantic import BaseModel

from database import filesystem_database
from database.filesystem_database import FilesystemDatabase
from models.database_filesystem_config import DatabaseFilesystemConfig

LOGGER = "database.filesystem_database"


@pytest.fixture
def base(tmp_path):
    return tmp_path / "fs"


@pytest.fixture
def db(base):
    return FilesystemDatabase(DatabaseFilesystemConfig(database_dir=str(base)))


def _read(base, table, key):
    return json.loads((base / table / key).read_text())


def test_init_creates_configured_dir(base, db):
    assert base.is_dir()


def test_init_defaults_to_data_filesystem_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    database = FilesystemDatabase(DatabaseFilesystemConfig())
    assert database.base_dir == os.path.join("data", "filesystem_db")
    assert (tmp_path / "data" / "filesystem_db").is_dir()


def test_upsert_overwrites(base, db):
    db.upsert_item("t", "k", {"v": 1})
    db.upsert_item("t", "k", {"v": 2})
    assert _read(base, "t", "k") == {"v": 2}


def test_insert_failure_is_logged_and_raised(base, db, caplog):
    (base / "t" / "k").mkdir(parents=True)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        with pytest.raises(IsADirectoryError):
            db.insert_item("t", "k", {"v": 1})
    assert "Failed to insert item" in caplog.text


def test_get_item_corrupt_json_raises(base, db):
    (base / "t").mkdir(parents=True)
    (base / "t" / "bad").write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        db.get_item("t", "bad")


def test_get_binary_item(base, db):
    (base / "t").mkdir(parents=True)
    (base / "t" / "blob").write_bytes(b"\x00\xffpayload")
    (base / "t" / "dir").mkdir()

    assert db.get_binary_item("t", "blob") == b"\x00\xffpayload"
    assert db.get_binary_item("t", "missing") == {}
    with pytest.raises(IsADirectoryError):
        db.get_binary_item("t", "dir")


def test_get_all_items_corrupt_file_raises(base, db):
    db.insert_item("t", "good", {"v": 1})
    (base / "t" / "bad").write_text("nope")
    with pytest.raises(json.JSONDecodeError):
        db.get_all_items("t")


def test_update_item_missing_returns_empty(db, base):
    assert db.update_item("t", "ghost", {"v": 1}) == {}
    assert not (base / "t" / "ghost").exists()


def test_update_item_null_handling(base, db):
    db.insert_item("t", "k", {"id": "k", "v": 1, "w": 2})

    assert db.update_item("t", "k", {"id": "other", "v": None}) == {"id": "k", "v": 1, "w": 2}
    assert _read(base, "t", "k") == {"id": "k", "v": 1, "w": 2}

    db.update_item("t", "k", {"v": None}, include_nulls=True)
    assert _read(base, "t", "k") == {"id": "k", "v": None, "w": 2}


def test_update_item_write_failure_is_raised(base, db, monkeypatch):
    db.insert_item("t", "k", {"v": 1})

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(filesystem_database.json, "dump", boom)
    with pytest.raises(OSError, match="disk full"):
        db.update_item("t", "k", {"v": 2})


def test_delete_missing_warns_and_delete_failure_raises(base, db, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        db.delete_item("t", "ghost")
    assert "nothing to delete" in caplog.text

    db.insert_item("t", "k", {"v": 1})

    def deny(_path):
        raise PermissionError("read-only")

    monkeypatch.setattr(filesystem_database.os, "remove", deny)
    with pytest.raises(PermissionError):
        db.delete_item("t", "k")
    assert (base / "t" / "k").exists()


def test_search_by_key_part_regex_skips_rows_without_id(db):
    db.insert_item("t", "a1", {"id": "a1"})
    db.insert_item("t", "ba", {"id": "ba"})
    db.insert_item("t", "noid", {"v": 1})
    assert {i["id"] for i in db.search_by_key_part("t", "a$|^a", regex=True)} == {"a1", "ba"}
    assert {i["id"] for i in db.search_by_key_part("t", "a")} == {"a1"}


# ── insert_columns ────────────────────────────────────────────────────────


def test_insert_columns_length_mismatch(db):
    with pytest.raises(ValueError, match="must match"):
        db.insert_columns("t", ["a", "b"], [1])


def test_insert_columns_generates_key_when_no_id(base, db):
    db.insert_columns("t", ["a"], [1])
    [key] = os.listdir(base / "t")
    assert _read(base, "t", key) == {"a": 1, "id": key}


def test_insert_columns_conflict_strategies(base, db, caplog):
    db.insert_columns("t", ["id", "v"], [7, "first"])
    db.insert_columns("t", ["id", "v"], [7, "ignored"])
    assert _read(base, "t", "7")["v"] == "first"

    db.insert_columns("t", ["id", "v"], [7, "replaced"], conflict_strategy="REPLACE")
    assert _read(base, "t", "7")["v"] == "replaced"

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        db.insert_columns("t", ["id", "v"], [8, "new"], conflict_strategy="ABORT")
    assert "Unsupported conflict strategy 'ABORT'" in caplog.text
    assert _read(base, "t", "8")["v"] == "new"


def test_insert_columns_propagates_write_failure(db, monkeypatch, caplog):
    def fail(*_args):
        raise OSError("no space")

    monkeypatch.setattr(db, "insert_item", fail)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        with pytest.raises(OSError, match="no space"):
            db.insert_columns("t", ["id"], ["x"])
    assert "Failed to insert into t" in caplog.text


# ── ensure_table ─────────────────────────────────────────────────────────


class OrderLine(BaseModel):
    id: str
    qty: int


def test_ensure_table_writes_metadata_once(base, db):
    db.ensure_table(OrderLine)
    meta_path = base / "order_line" / ".table_metadata.json"
    meta = json.loads(meta_path.read_text())
    assert meta["table_name"] == "order_line"
    assert meta["model"] == "OrderLine"
    assert meta["fields"] == {"id": "<class 'str'>", "qty": "<class 'int'>"}

    meta_path.write_text('{"sentinel": true}')
    db.ensure_table(OrderLine)
    assert json.loads(meta_path.read_text()) == {"sentinel": True}


def test_ensure_table_tolerates_unreadable_fields(base, db, caplog):
    class Opaque:
        __fields__ = None

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        db.ensure_table(Opaque, table_name="opaque")
    assert json.loads((base / "opaque" / ".table_metadata.json").read_text())["fields"] == {}
    assert "Could not extract field info for model Opaque" in caplog.text


def test_ensure_table_directory_failure_is_raised(db, monkeypatch, caplog):
    def fail(_table):
        raise OSError("read-only fs")

    monkeypatch.setattr(db, "_get_table_dir", fail)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        with pytest.raises(OSError):
            db.ensure_table(OrderLine)
    assert "Error ensuring filesystem table for model OrderLine" in caplog.text
