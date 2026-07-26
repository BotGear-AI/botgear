"""Tests for the query_page pagination primitive (LIMIT/OFFSET/ORDER BY + total).

Covers:
  * the in-memory base default (via TinyDB, which does not override it),
  * the SQLite native push-down (and its no-filter-only push, filter fallback),
  * the Postgres SQL builder — identifier safety for the order_by column and
    bound limit/offset params, introspected without a live database.
"""
import os
import tempfile
import shutil
from unittest.mock import MagicMock

import pytest
from psycopg2 import sql as pgsql

from database.interface import DatabaseAdapter
from database.sqlite_database import SqliteDatabase
from database.postgres_database import PostgresDatabase
from models.database_sqlite_config import DatabaseSqliteConfig
from pydantic import BaseModel


class _Row(BaseModel):
    id: str
    n: int | None = None
    grp: str | None = None


def _seed(db, table, n=25):
    for i in range(n):
        db.insert_item(table, f"id-{i:03d}", {"id": f"id-{i:03d}", "n": i, "grp": "even" if i % 2 == 0 else "odd"})


# ── base default (TinyDB does not override query_page) ─────────────────────

class _InMemory(DatabaseAdapter):
    """Smallest object satisfying the base query_page (only needs query_items).

    Represents every adapter that does NOT override query_page (tinydb, s3,
    filesystem, dynamodb) — they all inherit this exact in-memory behavior.
    """
    def __init__(self, rows):
        self._rows = rows
    def query_items(self, table_name, criteria):
        return [r for r in self._rows if all(r.get(k) == v for k, v in (criteria or {}).items())]
    def upsert_item(self, *a, **k): ...
    def insert_item(self, *a, **k): ...
    def get_item(self, *a, **k): ...
    def get_binary_item(self, *a, **k): ...
    def get_all_items(self, table): return list(self._rows)
    def update_item(self, *a, **k): ...
    def delete_item(self, *a, **k): ...
    def search_by_key_part(self, *a, **k): ...
    def copy_table(self, *a, **k): ...
    def query(self, *a, **k): ...
    def insert_columns(self, *a, **k): ...
    def ensure_table(self, *a, **k): ...


@pytest.fixture
def mem():
    return _InMemory([{"id": f"id-{i:03d}", "n": i, "grp": "even" if i % 2 == 0 else "odd"} for i in range(25)])


def test_base_default_paginates_and_counts(mem):
    rows, total = mem.query_page("widget", order_by="n", limit=10, offset=0)
    assert total == 25
    assert [r["n"] for r in rows] == list(range(10))


def test_base_default_offset_and_descending(mem):
    rows, total = mem.query_page("widget", order_by="n", descending=True, limit=5, offset=5)
    assert total == 25
    assert [r["n"] for r in rows] == [19, 18, 17, 16, 15]


def test_base_default_filter_narrows_total(mem):
    rows, total = mem.query_page("widget", filters={"grp": "even"}, order_by="n", limit=3)
    assert total == 13  # 0,2,...,24
    assert [r["n"] for r in rows] == [0, 2, 4]


def test_base_default_offset_past_end_is_empty_but_total_intact(mem):
    rows, total = mem.query_page("widget", order_by="n", limit=10, offset=100)
    assert total == 25
    assert rows == []


# ── SQLite via the inherited base default (no override — see adapter note) ──
# Verifies the base query_page produces correct pages on a real adapter with
# Python-typed ordering (SQLite intentionally does NOT push down to avoid
# TEXT-affinity sort divergence).

@pytest.fixture
def sqlite_db():
    d = tempfile.mkdtemp()
    db = SqliteDatabase(DatabaseSqliteConfig(sqlite_path=os.path.join(d, "t.sqlite3")))
    db.ensure_table(_Row, table_name="widget")
    _seed(db, "widget", 25)
    yield db
    shutil.rmtree(d, ignore_errors=True)
    SqliteDatabase._instances.clear()


def test_sqlite_page_numeric_order(sqlite_db):
    rows, total = sqlite_db.query_page("widget", order_by="n", limit=10, offset=10)
    assert total == 25
    assert [r["n"] for r in rows] == list(range(10, 20))


def test_sqlite_offset_without_limit(sqlite_db):
    rows, total = sqlite_db.query_page("widget", order_by="n", offset=23)
    assert total == 25
    assert [r["n"] for r in rows] == [23, 24]


def test_sqlite_filter_narrows_total(sqlite_db):
    rows, total = sqlite_db.query_page("widget", filters={"grp": "odd"}, order_by="n", limit=3)
    assert total == 12
    assert [r["n"] for r in rows] == [1, 3, 5]


# ── Postgres SQL builder (no live db) ──────────────────────────────────────

class _StubPgDb(PostgresDatabase):
    def __init__(self):
        self.config = None
        self.sslmode = "prefer"
        self._pool = None


def _walk_identifiers(node):
    if isinstance(node, pgsql.Identifier):
        yield from node.strings
    elif isinstance(node, pgsql.Composed):
        for c in node.seq:
            yield from _walk_identifiers(c)


def _walk_sql(node):
    if isinstance(node, pgsql.SQL):
        yield node.string
    elif isinstance(node, pgsql.Composed):
        for c in node.seq:
            yield from _walk_sql(c)


class TestPostgresQueryPageBuilder:
    def setup_method(self):
        self.db = _StubPgDb()
        self.cur = MagicMock()
        self.cur.fetchone.return_value = {"total": 0}
        self.cur.fetchall.return_value = []

        class _Ctx:
            def __init__(_, c): _._c = c
            def __enter__(_): return _._c
            def __exit__(_, *a): return False

        self.db._get_cursor = MagicMock(return_value=_Ctx(self.cur))

    def test_order_by_is_identifier_never_raw(self):
        self.db.query_page("run", order_by="start_datetime", descending=True, limit=25, offset=50)
        # The page SELECT is the last execute call.
        stmt = self.cur.execute.call_args.args[0]
        idents = list(_walk_identifiers(stmt))
        assert "run" in idents and "start_datetime" in idents
        for chunk in _walk_sql(stmt):
            assert "start_datetime" not in chunk and "run" not in chunk

    def test_limit_offset_are_bound_params(self):
        self.db.query_page("run", order_by="n", limit=25, offset=50)
        params = self.cur.execute.call_args.args[1]
        assert params == [25, 50]

    def test_malicious_order_by_rejected(self):
        with pytest.raises(ValueError):
            self.db.query_page("run", order_by="n; DROP TABLE run")

    def test_filter_columns_validated(self):
        with pytest.raises(ValueError):
            self.db.query_page("run", filters={"n; DROP TABLE run": 1})
