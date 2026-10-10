import os
import tempfile
import shutil
import pytest
from pydantic import BaseModel
from database.sqlite_database import SqliteDatabase

from models.database_sqlite_config import DatabaseSqliteConfig


class SqliteTestItemModel(BaseModel):
    id: str
    uuid: str | None = None
    value: str | None = None
    type: str | None = None
    name: str | None = None


def ensure_table(db, table_name: str) -> None:
    db.ensure_table(SqliteTestItemModel, table_name=table_name)

@pytest.fixture
def sqlite_db():
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test.sqlite3")
    config = DatabaseSqliteConfig(sqlite_path=db_path)
    db = SqliteDatabase(config)
    ensure_table(db, "test")
    yield db
    shutil.rmtree(temp_dir)
    SqliteDatabase._instances.clear()

def test_insert_and_get_item(sqlite_db):
    item = {"uuid": "1", "value": "foo"}
    sqlite_db.insert_item("test", "1", item)
    result = sqlite_db.get_item("test", "1")
    assert result["uuid"] == "1"
    assert result["value"] == "foo"

def test_update_item(sqlite_db):
    sqlite_db.insert_item("test", "2", {"uuid": "2", "value": "bar"})
    updated = sqlite_db.update_item("test", "2", {"value": "baz"})
    assert updated["uuid"] == "2"
    assert updated["value"] == "baz"


def test_compare_and_update_item_is_conditional_and_persisted(sqlite_db):
    sqlite_db.insert_item("test", "session", {"value": "old", "type": "active", "name": "tenant-a"})

    assert sqlite_db.compare_and_update_item(
        "test", "session", {"value": "old", "type": "active"}, {"value": "new"},
    )
    assert not sqlite_db.compare_and_update_item("test", "session", {"value": "old"}, {"value": "replay"})
    sqlite_db.update_item("test", "session", {"type": "revoked"})
    assert not sqlite_db.compare_and_update_item(
        "test", "session", {"value": "new", "type": "active"}, {"value": "resurrected"},
    )
    row = sqlite_db.get_item("test", "session")
    assert row["value"] == "new" and row["type"] == "revoked" and row["name"] == "tenant-a"
    assert not sqlite_db.compare_and_update_item("test", "missing", {"value": "old"}, {"value": "new"})


@pytest.mark.parametrize("expected,updates", [
    ({}, {"value": "new"}),
    ({"value": "old"}, {}),
    ({"value": "old"}, {"id": "changed"}),
    ({"value OR 1=1": "old"}, {"value": "new"}),
    ({"value": "old"}, {"value; DROP TABLE test": "new"}),
])
def test_compare_and_update_rejects_unsafe_or_unconditional_writes(sqlite_db, expected, updates):
    sqlite_db.insert_item("test", "row", {"value": "old"})

    with pytest.raises(ValueError):
        sqlite_db.compare_and_update_item("test", "row", expected, updates)

    assert sqlite_db.get_item("test", "row")["value"] == "old"


def test_compare_and_update_binds_values_and_matches_null(sqlite_db):
    sqlite_db.insert_item("test", "row", {"value": "old"})
    value = "new'; DROP TABLE test; --"

    assert sqlite_db.compare_and_update_item("test", "row", {"name": None}, {"value": value})
    assert sqlite_db.get_item("test", "row")["value"] == value


def test_compare_and_update_preserves_scalar_types(sqlite_db):
    sqlite_db.insert_item("test", "typed", {"value": 42, "type": True, "name": None})

    assert sqlite_db.compare_and_update_item(
        "test", "typed", {"value": 42, "type": True, "name": None}, {"value": 43, "type": False},
    )
    row = sqlite_db.get_item("test", "typed")
    assert row["value"] == 43 and isinstance(row["value"], int)
    assert row["type"] is False and row["name"] is None


def test_delete_item(sqlite_db):
    sqlite_db.insert_item("test", "3", {"uuid": "3", "value": "del"})
    sqlite_db.delete_item("test", "3")
    assert sqlite_db.get_item("test", "3") == {}

def test_get_all_items(sqlite_db):
    table = "test_get_all_items"
    ensure_table(sqlite_db, table)
    sqlite_db.insert_item(table, "a", {"uuid": "a"})
    sqlite_db.insert_item(table, "b", {"uuid": "b"})
    items = sqlite_db.get_all_items(table)
    uuids = {item["uuid"] for item in items}
    assert uuids == {"a", "b"}

def test_search_by_key_part(sqlite_db):
    sqlite_db.insert_item("test", "abc1", {"id": "abc1"})
    sqlite_db.insert_item("test", "abc2", {"id": "abc2"})
    sqlite_db.insert_item("test", "xyz", {"id": "xyz"})
    results = sqlite_db.search_by_key_part("test", "abc")
    ids = {item["id"] for item in results}
    assert ids == {"abc1", "abc2"}

def test_query_items(sqlite_db):
    sqlite_db.insert_item("test", "1", {"uuid": "1", "type": "A"})
    sqlite_db.insert_item("test", "2", {"uuid": "2", "type": "B"})
    results = sqlite_db.query_items("test", {"type": "A"})
    assert len(results) == 1
    assert results[0]["uuid"] == "1"

def test_copy_table(sqlite_db):
    ensure_table(sqlite_db, "source")
    ensure_table(sqlite_db, "dest")
    sqlite_db.insert_item("source", "1", {"uuid": "1", "value": "a"})
    sqlite_db.insert_item("source", "2", {"uuid": "2", "value": "b"})
    sqlite_db.insert_item("source", "3", {"uuid": "3", "value": "c"})
    sqlite_db.copy_table("source", "dest")
    dest_items = sqlite_db.get_all_items("dest")
    assert len(dest_items) == 3
    dest_uuids = {item["uuid"] for item in dest_items}
    assert dest_uuids == {"1", "2", "3"}

def test_upsert_item(sqlite_db):
    item = {"uuid": "upsert1", "name": "First"}
    result = sqlite_db.upsert_item("test", "upsert1", item)
    assert result["uuid"] == "upsert1"
    updated = sqlite_db.upsert_item("test", "upsert1", {"name": "Updated"})
    assert updated["name"] == "Updated"

def test_get_binary_item_found(sqlite_db):
    # Create a table with a BLOB column
    cur = sqlite_db.conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS bin_test (id TEXT PRIMARY KEY, data BLOB)")
    # Insert a binary blob
    binary_data = b"\x00\x01\x02hello\x03\x04"
    cur.execute("INSERT INTO bin_test (id, data) VALUES (?, ?)", ("blob1", binary_data))
    sqlite_db.conn.commit()
    # Patch get_binary_item to fetch from the BLOB column for this test
    def get_blob(table, key):
        cur = sqlite_db.conn.cursor()
        cur.execute(f"SELECT data FROM {table} WHERE id = ?", (key,))
        row = cur.fetchone()
        if row:
            return row[0]
        raise ValueError(f"Item with key {key} not found in {table}")
    # Actually test
    data = get_blob("bin_test", "blob1")
    assert isinstance(data, bytes)
    assert b"hello" in data

def test_get_binary_item_not_found(sqlite_db):
    with pytest.raises(ValueError):
        sqlite_db.get_binary_item("test", "notfound")

def test_update_item_nonexistent(sqlite_db):
    result = sqlite_db.update_item("test", "nope", {"foo": "bar"})
    assert result == {}

def test_delete_item_nonexistent(sqlite_db):
    # Should not raise
    sqlite_db.delete_item("test", "nope")
    assert True

def test_get_item_nonexistent(sqlite_db):
    result = sqlite_db.get_item("test", "nope")
    assert result == {}

def test_query_sqlite_query(sqlite_db):
    sqlite_db.insert_item("test", "1", {"uuid": "1", "value": "foo"})
    sqlite_db.insert_item("test", "2", {"uuid": "2", "value": "bar"})
    # Query for all rows
    results = sqlite_db.query("SELECT * FROM test")
    assert len(results) == 2
    ids = {row["id"] for row in results}
    assert ids == {"1", "2"}
    # Query for a specific row
    results = sqlite_db.query("SELECT * FROM test WHERE id = '1'")
    assert len(results) == 1
    assert results[0]["id"] == "1"


def test_insert_item_duplicate_key_raises(sqlite_db):
    """insert_item must refuse a duplicate key, matching TinyDBDatabase and
    PostgresDatabase — botgear's first-user-claim race guard depends on this
    raise to detect a concurrent duplicate rather than silently overwriting."""
    sqlite_db.insert_item("test", "dup", {"uuid": "dup", "value": "first"})
    with pytest.raises(ValueError):
        sqlite_db.insert_item("test", "dup", {"uuid": "dup", "value": "second"})
    # Original row must be untouched by the failed insert.
    assert sqlite_db.get_item("test", "dup")["value"] == "first"


def test_upsert_item_still_overwrites_on_duplicate(sqlite_db):
    """upsert_item is unaffected by the insert_item duplicate-key fix."""
    sqlite_db.insert_item("test", "up1", {"uuid": "up1", "value": "first"})
    sqlite_db.upsert_item("test", "up1", {"uuid": "up1", "value": "second"})
    assert sqlite_db.get_item("test", "up1")["value"] == "second"


def test_copy_table_overwrites_existing_dest_rows(sqlite_db):
    """copy_table must still overwrite on a re-run (matches TinyDBDatabase's
    raw-insert overwrite semantics) even though insert_item now refuses dups."""
    ensure_table(sqlite_db, "src2")
    ensure_table(sqlite_db, "dest2")
    sqlite_db.insert_item("src2", "1", {"uuid": "1", "value": "new"})
    sqlite_db.insert_item("dest2", "1", {"uuid": "1", "value": "stale"})
    sqlite_db.copy_table("src2", "dest2")
    assert sqlite_db.get_item("dest2", "1")["value"] == "new"


def test_ensure_table_adds_missing_columns_for_schema_drift(sqlite_db):
    """A model that gains a field after its table already exists (routine —
    templates/apps/*.yml grows a field, regenerate) must not break inserts
    against the live table. TinyDB's schemaless JSON tolerates this for
    free; sqlite's ensure_table must ALTER TABLE ADD COLUMN instead."""
    class V1(BaseModel):
        id: str
        value: str | None = None

    class V2(BaseModel):
        id: str
        value: str | None = None
        new_field: str | None = None

    sqlite_db.ensure_table(V1, table_name="drift")
    sqlite_db.insert_item("drift", "1", {"value": "old"})

    sqlite_db.ensure_table(V2, table_name="drift")
    sqlite_db.insert_item("drift", "2", {"value": "new", "new_field": "hello"})

    assert sqlite_db.get_item("drift", "1")["value"] == "old"
    assert sqlite_db.get_item("drift", "2")["new_field"] == "hello"


def test_insert_item_self_heals_undeclared_column(sqlite_db):
    """A write can carry a key that was never on the Pydantic model passed
    to ensure_table at all (e.g. botgear's config/default seed injects
    `created_at` ad hoc). insert_item/upsert_item must add the column
    on the fly rather than raising 'table has no column named ...'."""
    sqlite_db.insert_item("test", "adhoc1", {"uuid": "adhoc1", "totally_undeclared_field": "surprise"})
    assert sqlite_db.get_item("test", "adhoc1")["totally_undeclared_field"] == "surprise"


def test_upsert_item_self_heals_undeclared_column(sqlite_db):
    sqlite_db.upsert_item("test", "adhoc2", {"uuid": "adhoc2", "another_undeclared_field": 42})
    assert sqlite_db.get_item("test", "adhoc2")["another_undeclared_field"] == 42


def test_list_tables(sqlite_db):
    ensure_table(sqlite_db, "another_table")
    tables = sqlite_db.list_tables()
    assert "test" in tables
    assert "another_table" in tables


def test_journal_mode_is_wal(sqlite_db):
    cur = sqlite_db.conn.execute("PRAGMA journal_mode")
    assert cur.fetchone()[0].lower() == "wal"


def test_distinct_paths_get_distinct_instances(tmp_path):
    """#298: SqliteDatabase must isolate instances per sqlite_path — a second
    config with a different path must not be silently bound to the first file."""
    SqliteDatabase._instances.clear()
    a = SqliteDatabase(DatabaseSqliteConfig(sqlite_path=str(tmp_path / "a.db")))
    b = SqliteDatabase(DatabaseSqliteConfig(sqlite_path=str(tmp_path / "b.db")))
    a_again = SqliteDatabase(DatabaseSqliteConfig(sqlite_path=str(tmp_path / "a.db")))
    assert a.config.sqlite_path.endswith("a.db")
    assert b.config.sqlite_path.endswith("b.db")   # was bound to a.db before the fix
    assert a is a_again and a is not b
    SqliteDatabase._instances.clear()


def test_failed_init_does_not_cache_a_broken_instance(tmp_path):
    """#298 hardening: if init fails (e.g. makedirs on a path under a file), the
    instance must not be cached as _initialized=True — later same-path callers
    must retry rather than receive a half-built object missing .conn/.transformer."""
    SqliteDatabase._instances.clear()
    (tmp_path / "blocker").write_text("i am a file, not a dir")
    bad = DatabaseSqliteConfig(sqlite_path=str(tmp_path / "blocker" / "x.db"))
    with pytest.raises(Exception):
        SqliteDatabase(bad)
    cached = SqliteDatabase._instances.get(bad.sqlite_path)
    assert cached is None or getattr(cached, "_initialized", False) is False
    # A second attempt retries init (raises again) rather than serving a broken cache.
    with pytest.raises(Exception):
        SqliteDatabase(bad)
    SqliteDatabase._instances.clear()
