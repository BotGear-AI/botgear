"""Error arms and schema branches of SqliteDatabase not reached by the CRUD tests."""
import json
import logging
import sqlite3
from typing import Dict, List, Optional, Union

import pytest
from pydantic import BaseModel, Field

from database.sqlite_database import SqliteDatabase
from models.database_sqlite_config import DatabaseSqliteConfig


class Item(BaseModel):
    id: str
    name: Optional[str] = None
    count: Optional[int] = None


@pytest.fixture
def db(tmp_path):
    SqliteDatabase._instances.clear()
    database = SqliteDatabase(DatabaseSqliteConfig(sqlite_path=str(tmp_path / "t.sqlite3")))
    database.ensure_table(Item, table_name="items")
    yield database
    database.conn.close()
    SqliteDatabase._instances.clear()


def _raw(db, statement, params=()):
    conn = db._get_connection()
    try:
        rows = conn.execute(statement, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _columns(db, table):
    return {row[1]: {"type": row[2], "notnull": row[3]} for row in _raw(db, f'PRAGMA table_info("{table}")')}


def test_bare_filename_path_creates_db_in_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    SqliteDatabase._instances.clear()
    try:
        database = SqliteDatabase(DatabaseSqliteConfig(sqlite_path="bare.sqlite3"))
        database.conn.close()
        assert (tmp_path / "bare.sqlite3").is_file()
    finally:
        SqliteDatabase._instances.clear()


# ── get_binary_item ───────────────────────────────────────────────────────


def test_get_binary_item_missing_table(db):
    with pytest.raises(ValueError, match="does not exist"):
        db.get_binary_item("no_such_table", "k")


def test_get_binary_item_reads_json_column(db):
    _raw(db, 'CREATE TABLE blobs (id TEXT PRIMARY KEY, json BLOB)')
    _raw(db, "INSERT INTO blobs VALUES (?, ?), (?, ?), (?, ?)",
         ("text", '{"a": 1}', "bytes", b"\x00\x01raw", "null", None))

    assert db.get_binary_item("blobs", "text") == b'{"a": 1}'
    assert db.get_binary_item("blobs", "bytes") == b"\x00\x01raw"
    with pytest.raises(ValueError, match="not found"):
        db.get_binary_item("blobs", "null")
    with pytest.raises(ValueError, match="not found"):
        db.get_binary_item("blobs", "absent")


def test_get_binary_item_serializes_row_without_json_column(db):
    _raw(db, 'CREATE TABLE records (id TEXT PRIMARY KEY, payload BLOB, n INTEGER)')
    _raw(db, "INSERT INTO records VALUES (?, ?, ?)", ("r1", b"bytes-here", 7))

    assert json.loads(db.get_binary_item("records", "r1")) == {"id": "r1", "payload": "bytes-here", "n": 7}
    with pytest.raises(ValueError, match="not found"):
        db.get_binary_item("records", "r2")


# ── get_all_items / update_item / search ─────────────────────────────────


@pytest.mark.parametrize("table", ["bad-name; DROP TABLE items", "missing_table"])
def test_get_all_items_errors_return_empty_list(db, caplog, table):
    with caplog.at_level(logging.ERROR, logger="database.sqlite_database"):
        assert db.get_all_items(table) == []
    assert f"Error retrieving all items from {table}" in caplog.text


def test_update_item_include_nulls_writes_none(db):
    db.insert_item("items", "1", {"name": "a", "count": 5})
    db.update_item("items", "1", {"count": None}, include_nulls=True)
    assert db.get_item("items", "1")["count"] is None


def test_update_item_without_applicable_fields_is_a_no_op(db, caplog):
    db.insert_item("items", "1", {"name": "a", "count": 5})
    with caplog.at_level(logging.WARNING, logger="database.sqlite_database"):
        result = db.update_item("items", "1", {"id": "hijack", "count": None})
    assert result["count"] == 5
    assert db.get_item("items", "1")["count"] == 5
    assert db.get_item("items", "hijack") == {}
    assert "no applicable fields" in caplog.text


def test_search_by_key_part_regex_vs_prefix(db):
    for key in ("abc1", "xabc", "zzz"):
        db.insert_item("items", key, {"name": key})
    assert {i["id"] for i in db.search_by_key_part("items", "abc")} == {"abc1"}
    assert {i["id"] for i in db.search_by_key_part("items", "abc", regex=True)} == {"abc1", "xabc"}
    assert db.search_by_key_part("items", "^z+$", regex=True)[0]["id"] == "zzz"


def test_query_non_select_statement_is_committed(db):
    db.insert_item("items", "1", {"name": "doomed"})
    assert db.query("DELETE FROM items WHERE id = '1'") == []
    assert db.get_item("items", "1") == {}


# ── insert_columns ────────────────────────────────────────────────────────


def test_insert_columns_validates_arguments(db):
    with pytest.raises(ValueError, match="must match"):
        db.insert_columns("items", ["id", "name"], ["1"])
    with pytest.raises(ValueError, match="conflict_strategy"):
        db.insert_columns("items", ["id"], ["1"], conflict_strategy="IGNORE; DROP TABLE items")
    with pytest.raises(ValueError, match="invalid column"):
        db.insert_columns("items", ["id", "name) VALUES (1); --"], ["1", "x"])
    assert db.get_all_items("items") == []


def test_insert_columns_conflict_strategies(db):
    db.insert_columns("items", ["id", "name"], ["1", "original"])
    db.insert_columns("items", ["id", "name"], ["1", "ignored"])
    assert db.get_item("items", "1")["name"] == "original"
    db.insert_columns("items", ["id", "name"], ["1", "replaced"], conflict_strategy="REPLACE")
    assert db.get_item("items", "1")["name"] == "replaced"


def test_insert_columns_engine_error_is_logged_and_raised(db, caplog):
    with caplog.at_level(logging.ERROR, logger="database.sqlite_database"):
        with pytest.raises(sqlite3.OperationalError):
            db.insert_columns("items", ["id", "no_such_column"], ["1", "x"])
    assert "Failed to insert into items" in caplog.text


# ── ensure_table ─────────────────────────────────────────────────────────


class WidgetThing(BaseModel):
    id: str
    title: str
    maybe: Optional[int]
    either: Union[int, str]
    ratio: float = 0.5
    flag: bool
    tags: List[str] = Field(default_factory=list)
    attrs: Dict[str, int] = Field(default_factory=dict)


def test_ensure_table_default_name_types_and_nullability(db):
    db.ensure_table(WidgetThing)
    cols = _columns(db, "widget_thing")

    assert cols["id"]["type"] == "TEXT"
    assert (cols["title"]["type"], cols["title"]["notnull"]) == ("TEXT", 1)
    # Required but Optional[...] -> nullable via the Union/None check.
    assert (cols["maybe"]["type"], cols["maybe"]["notnull"]) == ("INTEGER", 0)
    # Union without None stays NOT NULL and takes the first arm's type.
    assert (cols["either"]["type"], cols["either"]["notnull"]) == ("INTEGER", 1)
    assert (cols["ratio"]["type"], cols["ratio"]["notnull"]) == ("REAL", 0)
    assert (cols["flag"]["type"], cols["flag"]["notnull"]) == ("INTEGER", 1)
    assert (cols["tags"]["type"], cols["tags"]["notnull"]) == ("TEXT", 0)
    assert (cols["attrs"]["type"], cols["attrs"]["notnull"]) == ("TEXT", 0)


class _LegacyField:
    def __init__(self, annotation, is_required):
        self.annotation = annotation
        self.is_required = is_required


class LegacyModel:
    """Pydantic-v1-shaped model: __fields__ only, is_required as an attribute."""
    __fields__ = {
        "title": _LegacyField(str, True),
        "note": _LegacyField(str, False),
        "raw": int,
    }


def test_ensure_table_legacy_fields_shape(db):
    db.ensure_table(LegacyModel, table_name="legacy")
    cols = _columns(db, "legacy")
    assert (cols["title"]["type"], cols["title"]["notnull"]) == ("TEXT", 1)
    assert (cols["note"]["type"], cols["note"]["notnull"]) == ("TEXT", 0)
    assert (cols["raw"]["type"], cols["raw"]["notnull"]) == ("INTEGER", 1)


def test_ensure_table_rejects_unsafe_field_and_logs(db, caplog):
    class Unsafe:
        __fields__ = {"bad name": _LegacyField(str, True)}

    with caplog.at_level(logging.ERROR, logger="database.sqlite_database"):
        with pytest.raises(ValueError, match="invalid column"):
            db.ensure_table(Unsafe, table_name="unsafe")
    assert "Error ensuring table for model Unsafe" in caplog.text
    assert _columns(db, "unsafe") == {}


class _OnlyNoneUnion:
    __origin__ = Union
    __args__ = (type(None),)


class _Custom:
    pass


@pytest.mark.parametrize(
    "python_type, expected",
    [
        (List[int], "TEXT"), (Dict[str, int], "TEXT"), (Optional[float], "REAL"),
        (Optional[bool], "INTEGER"), (_OnlyNoneUnion, "TEXT"), (_Custom, "TEXT"), (bytes, "TEXT"),
    ],
)
def test_get_sqlite_type(db, python_type, expected):
    assert db._get_sqlite_type(python_type) == expected
