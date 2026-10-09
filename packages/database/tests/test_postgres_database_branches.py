"""Pool recovery, error arms and SQL shape for PostgresDatabase, fully offline.

The pool, connection and cursor are mocks; statements are captured and rendered
from psycopg2's sql.Composable tree (no live connection is needed to read them).
"""
import logging
from types import SimpleNamespace
from typing import Dict, List, Optional, Union
from unittest.mock import MagicMock, patch

import psycopg2
import pytest
from psycopg2 import OperationalError, sql
from psycopg2.pool import PoolError
from pydantic import BaseModel

from database.postgres_database import PostgresDatabase
from models.database_postgres_config import DatabasePostgresConfig

LOGGER = "database.postgres_database"


def render(stmt) -> str:
    if isinstance(stmt, str):
        return stmt
    if isinstance(stmt, sql.Composed):
        return "".join(render(part) for part in stmt.seq)
    if isinstance(stmt, sql.Identifier):
        return ".".join(f'"{s}"' for s in stmt.strings)
    if isinstance(stmt, sql.Placeholder):
        return "%s"
    if isinstance(stmt, sql.SQL):
        return stmt.string
    raise TypeError(f"unexpected composable {stmt!r}")


@pytest.fixture
def pg():
    config = DatabasePostgresConfig(host="db.internal", user="u", password="p", database="d")
    with patch("database.postgres_database.ThreadedConnectionPool") as pool_cls, \
            patch("database.postgres_database.time.sleep") as sleep:
        pool, conn, cur = MagicMock(), MagicMock(), MagicMock()
        pool_cls.return_value = pool
        pool.getconn.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur
        state = SimpleNamespace(
            pool_cls=pool_cls, pool=pool, conn=conn, cur=cur, sleep=sleep,
            config=config, executed=[], fail=None,
        )

        def execute(stmt, params=None):
            text = render(stmt)
            if text == "SELECT 1":
                return
            if state.fail is not None:
                raise state.fail
            state.executed.append((text, params))

        cur.execute.side_effect = execute
        state.db = PostgresDatabase(config)
        yield state
        state.db._pool = None


# ── pool lifecycle ────────────────────────────────────────────────────────


@pytest.mark.parametrize("sslmode, expected", [(None, "prefer"), ("None", "prefer"), ("", "prefer"), ("require", "require")])
def test_sslmode_defaults_to_prefer(pg, sslmode, expected):
    db = PostgresDatabase(pg.config.model_copy(update={"sslmode": sslmode}))
    assert pg.pool_cls.call_args.kwargs["sslmode"] == expected
    assert pg.pool_cls.call_args.kwargs["dbname"] == "d"
    db._pool = None


def test_reconnect_replaces_pool_even_if_closing_old_one_fails(pg, caplog):
    new_pool = MagicMock()
    pg.pool.closeall.side_effect = Exception("already closed")
    pg.pool_cls.return_value = new_pool
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        pg.db._connect()
    assert pg.db._pool is new_pool
    assert "Error closing existing pool" in caplog.text


def test_connect_failure_clears_pool_and_raises(pg):
    pg.pool_cls.side_effect = OperationalError("could not connect")
    with pytest.raises(OperationalError):
        pg.db._connect()
    assert pg.db._pool is None


def test_discard_connection_none_is_a_no_op(pg):
    pg.db._discard_connection(None)
    pg.pool.putconn.assert_not_called()


def test_discard_connection_survives_every_cleanup_failure(pg, caplog):
    bad = MagicMock()
    bad.close.side_effect = Exception("socket gone")
    pg.pool.putconn.side_effect = Exception("pool closed")
    pg.pool_cls.side_effect = OperationalError("still down")

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        pg.db._discard_connection(bad)

    bad.close.assert_called_once()
    assert pg.db._pool is None
    assert "Error rebuilding pool after bad-connection return failure" in caplog.text


def test_return_connection_failure_closes_conn_and_rebuilds_pool(pg):
    new_pool = MagicMock()
    pg.pool.putconn.side_effect = Exception("pool closed")
    pg.pool_cls.return_value = new_pool
    conn = MagicMock()

    pg.db._return_connection(conn)

    conn.close.assert_called_once()
    assert pg.db._pool is new_pool


def test_return_connection_rebuild_failure_is_logged(pg, caplog):
    pg.pool.putconn.side_effect = Exception("pool closed")
    conn = MagicMock()
    conn.close.side_effect = Exception("also broken")
    pg.pool_cls.side_effect = OperationalError("down")
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        pg.db._return_connection(conn)
    assert "Error rebuilding pool after return failure" in caplog.text


def test_acquire_retries_when_pool_returns_no_connection(pg):
    pg.pool.getconn.side_effect = [None, pg.conn]
    assert pg.db._acquire_validated_connection() is pg.conn
    assert pg.pool.getconn.call_count == 2
    pg.sleep.assert_called_once_with(0.5)


def test_acquire_rebuilds_exhausted_pool_before_retry(pg):
    rebuilt = MagicMock()
    rebuilt.getconn.return_value = pg.conn
    pg.pool.getconn.side_effect = PoolError("connection pool exhausted")
    pg.pool_cls.return_value = rebuilt

    assert pg.db._acquire_validated_connection() is pg.conn
    assert pg.db._pool is rebuilt


def test_acquire_does_not_rebuild_for_other_pool_errors(pg):
    pg.pool.getconn.side_effect = [PoolError("trying to put unkeyed connection"), pg.conn]
    calls_before = pg.pool_cls.call_count
    assert pg.db._acquire_validated_connection() is pg.conn
    assert pg.pool_cls.call_count == calls_before


def test_acquire_logs_failed_exhausted_pool_rebuild(pg, caplog):
    pg.pool.getconn.side_effect = PoolError("connection pool exhausted")
    pg.pool_cls.side_effect = OperationalError("down")
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        with pytest.raises(Exception):
            pg.db._acquire_validated_connection()
    assert "Failed to rebuild exhausted pool" in caplog.text


def test_get_cursor_without_pool(pg):
    pg.db._pool = None
    with pytest.raises(OperationalError, match="not available"):
        with pg.db._get_cursor():
            pass
    pg.pool.getconn.assert_not_called()


def test_transaction_commits_and_returns_connection(pg):
    with pg.db.transaction() as conn:
        assert conn is pg.conn
        assert conn.autocommit is False

    pg.conn.commit.assert_called_once()
    pg.conn.rollback.assert_not_called()
    assert pg.conn.autocommit is True
    pg.pool.putconn.assert_called_with(pg.conn)


def test_transaction_rolls_back_and_returns_connection_on_failure(pg):
    error = RuntimeError("statement failed")

    with pytest.raises(RuntimeError, match="statement failed"):
        with pg.db.transaction():
            raise error

    pg.conn.rollback.assert_called_once()
    pg.conn.commit.assert_not_called()
    assert pg.conn.autocommit is True
    pg.pool.putconn.assert_called_once_with(pg.conn)


def test_transaction_preserves_original_error_when_rollback_fails(pg):
    pg.conn.rollback.side_effect = RuntimeError("rollback failed")

    with pytest.raises(RuntimeError, match="statement failed"):
        with pg.db.transaction():
            raise RuntimeError("statement failed")

    pg.conn.rollback.assert_called_once()
    pg.pool.putconn.assert_called_with(pg.conn, close=True)


def test_transaction_preserves_original_error_when_autocommit_reset_fails(pg):
    pg.db._restore_autocommit = MagicMock(side_effect=RuntimeError("reset failed"))
    original_cause = ValueError("root cause")

    with pytest.raises(RuntimeError, match="statement failed") as raised:
        with pg.db.transaction():
            raise RuntimeError("statement failed") from original_cause

    assert raised.value.__cause__ is original_cause
    pg.conn.rollback.assert_called_once()
    pg.pool.putconn.assert_called_once_with(pg.conn, close=True)


def test_transaction_returns_connection_on_base_exception(pg):
    with pytest.raises(KeyboardInterrupt):
        with pg.db.transaction():
            raise KeyboardInterrupt()

    pg.conn.rollback.assert_called_once()
    pg.pool.putconn.assert_called_once_with(pg.conn)


def test_transaction_discards_connection_when_commit_fails(pg):
    pg.conn.commit.side_effect = RuntimeError("commit failed")

    with pytest.raises(RuntimeError, match="commit failed"):
        with pg.db.transaction():
            pass

    pg.conn.rollback.assert_called_once()
    pg.pool.putconn.assert_called_once_with(pg.conn, close=True)


def test_transaction_without_pool(pg):
    pg.db._pool = None

    with pytest.raises(OperationalError, match="not available"):
        with pg.db.transaction():
            pass

    pg.pool.getconn.assert_not_called()


def test_close_is_idempotent_and_tolerates_closeall_errors(pg, caplog):
    pg.pool.closeall.side_effect = Exception("boom")
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        pg.db.close()
    assert pg.db._pool is None
    assert "Error closing database connection pool" in caplog.text
    pg.db.close()
    assert pg.pool.closeall.call_count == 1


def test_del_swallows_close_errors(pg, caplog):
    pg.db.close = MagicMock(side_effect=Exception("interpreter shutting down"))
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        pg.db.__del__()
    assert "Error during cleanup" in caplog.text


# ── helpers ───────────────────────────────────────────────────────────────


def test_render_sql_for_logging(pg):
    cur = MagicMock()
    with patch.object(logging.getLogger(LOGGER), "isEnabledFor", return_value=True):
        assert pg.db._render_sql_for_logging(cur, "SELECT 2") == "SELECT 2"
        cur.mogrify.side_effect = Exception("cannot mogrify")
        assert pg.db._render_sql_for_logging(cur, "SELECT %s", (1,)) is None


def test_infer_columns_arms(pg):
    columns = pg.db._infer_columns({
        "nothing": None, "zip": 2111, "count": 5, "score": 1.5, "label": "x", "blob": b"raw",
    })
    assert columns == {
        "nothing": "TEXT", "zip": "TEXT", "count": "BIGINT",
        "score": "DOUBLE PRECISION", "label": "TEXT", "blob": "TEXT",
    }


def test_prepare_values_logs_and_raises_on_unserializable(pg, caplog):
    circular = {}
    circular["self"] = circular
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        with pytest.raises(ValueError):
            pg.db._prepare_values({"id": "1", "payload": circular})
    assert "Error in _prepare_values with item keys ['id', 'payload']" in caplog.text


class _OnlyNoneUnion:
    __origin__ = Union
    __args__ = (type(None),)


class _Custom:
    pass


@pytest.mark.parametrize(
    "python_type, expected",
    [(List[int], "JSONB"), (Dict[str, int], "JSONB"), (Optional[bool], "BOOLEAN"),
     (Union[int, str], "BIGINT"), (_OnlyNoneUnion, "TEXT"), (_Custom, "TEXT")],
)
def test_get_postgres_type(pg, python_type, expected):
    assert pg.db._get_postgres_type(python_type) == expected


# ── write guards and SQL shape ────────────────────────────────────────────


@pytest.mark.parametrize("method", ["upsert_item", "insert_item"])
def test_writes_require_non_empty_item_with_id(pg, method):
    with pytest.raises(ValueError, match="empty item"):
        getattr(pg.db, method)("t", "k", {})
    with pytest.raises(ValueError, match="must contain 'id'"):
        getattr(pg.db, method)("t", "k", {"name": "x"})
    assert pg.executed == []


def test_upsert_sql_and_json_params(pg):
    pg.cur.fetchone.return_value = None
    assert pg.db.upsert_item("t", "1", {"id": "1", "tags": ["a"]}) is None
    [(text, params)] = pg.executed
    assert text == (
        'INSERT INTO "t" ("id", "tags") VALUES (%s, %s) '
        'ON CONFLICT (id) DO UPDATE SET "id"=EXCLUDED."id", "tags"=EXCLUDED."tags" RETURNING *'
    )
    assert params == ["1", '["a"]']


def test_update_item_without_updatable_fields_skips_sql(pg, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert pg.db.update_item("t", "k", {"id": "other", "name": None}) == {}
    assert pg.executed == []
    assert "no updatable fields" in caplog.text


def test_update_item_include_nulls(pg):
    pg.cur.fetchone.return_value = None
    assert pg.db.update_item("t", "k", {"name": None}, include_nulls=True) == {}
    assert pg.executed == [('UPDATE "t" SET "name" = %s WHERE id = %s RETURNING *', [None, "k"])]


def test_update_item_rejects_unsafe_column(pg):
    with pytest.raises(ValueError, match="invalid characters"):
        pg.db.update_item("t", "k", {"name = 'x' --": 1})
    assert pg.executed == []


def test_search_by_key_part_uses_bound_like_pattern(pg):
    pg.cur.fetchall.return_value = [{"id": "xaby"}]
    assert pg.db.search_by_key_part("t", "ab") == [{"id": "xaby"}]
    assert pg.executed == [('SELECT * FROM "t" WHERE id LIKE %s', ("%ab%",))]


def test_query_items_sql(pg):
    pg.cur.fetchall.return_value = [{"id": "1"}]
    assert pg.db.query_items("t", {}) == [{"id": "1"}]
    assert pg.db.query_items("t", {"status": "active", "n": 2}) == [{"id": "1"}]
    assert pg.executed == [
        ('SELECT * FROM "t"', None),
        ('SELECT * FROM "t" WHERE "status" = %s AND "n" = %s', ["active", 2]),
    ]


def test_query_page_with_filters_order_limit_offset(pg):
    pg.cur.fetchone.return_value = {"total": "42"}
    pg.cur.fetchall.return_value = [{"id": "a"}]

    rows, total = pg.db.query_page(
        "t", filters={"status": "active"}, order_by="created", descending=True, limit="10", offset=20,
    )

    assert (rows, total) == ([{"id": "a"}], 42)
    assert pg.executed == [
        ('SELECT COUNT(*) AS total FROM "t" WHERE "status" = %s', ["active"]),
        ('SELECT * FROM "t" WHERE "status" = %s ORDER BY "created" DESC LIMIT %s OFFSET %s', ["active", 10, 20]),
    ]


def test_query_page_ascending_without_limit(pg):
    pg.cur.fetchone.return_value = {"total": 0}
    pg.cur.fetchall.return_value = []
    assert pg.db.query_page("t", order_by="name") == ([], 0)
    assert pg.executed[1] == ('SELECT * FROM "t" ORDER BY "name" ASC', None)


def test_raw_query(pg):
    with pytest.raises(ValueError, match="cannot be empty"):
        pg.db.query("   ")

    pg.cur.description = None
    pg.cur.rowcount = 3
    assert pg.db.query("DELETE FROM t WHERE n = %s", (1,)) == [{"rowcount": 3}]

    pg.cur.description = [("id",)]
    pg.cur.fetchall.return_value = [{"id": "1"}]
    assert pg.db.query("SELECT id FROM t") == [{"id": "1"}]
    assert pg.executed == [("DELETE FROM t WHERE n = %s", (1,)), ("SELECT id FROM t", None)]


def test_get_binary_item(pg, caplog):
    pg.cur.fetchone.return_value = (b"\x00bytes",)
    assert pg.db.get_binary_item("t", "k") == b"\x00bytes"
    assert pg.executed == [('SELECT data FROM "t" WHERE id = %s', ("k",))]

    pg.cur.fetchone.return_value = None
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        with pytest.raises(ValueError, match="not found"):
            pg.db.get_binary_item("t", "missing")
    assert "Error in get_binary_item" in caplog.text


def test_list_tables(pg):
    pg.cur.fetchall.return_value = [("alpha",), ("beta",)]
    assert pg.db.list_tables() == ["alpha", "beta"]
    assert "FROM pg_tables" in pg.executed[0][0]


# ── error arms: every data method logs, discards the connection, re-raises ──


ERROR_CASES = [
    ("upsert_item", lambda db: db.upsert_item("t", "1", {"id": "1"}), "Error in upsert_item for table 't', key '1'"),
    ("insert_item", lambda db: db.insert_item("t", "1", {"id": "1"}), "Error in insert_item for table 't', key '1'"),
    ("get_item", lambda db: db.get_item("t", "1"), "Error in get_item"),
    ("get_all_items", lambda db: db.get_all_items("t"), "Error in get_all_items"),
    ("delete_item", lambda db: db.delete_item("t", "1"), "Error in delete_item"),
    ("update_item", lambda db: db.update_item("t", "1", {"n": 1}), "Error in update_item"),
    ("search_by_key_part", lambda db: db.search_by_key_part("t", "1"), "Error in search_by_key_part"),
    ("query_items", lambda db: db.query_items("t", {"n": 1}), "Error in query_items"),
    ("query_page", lambda db: db.query_page("t"), "Error in query_page"),
    ("query", lambda db: db.query("SELECT 2"), "Error in query execution"),
]


@pytest.mark.parametrize("name, call, message", ERROR_CASES, ids=[c[0] for c in ERROR_CASES])
def test_data_method_errors_are_logged_and_raised(pg, caplog, name, call, message):
    pg.fail = psycopg2.DatabaseError("relation does not exist")
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        with pytest.raises(psycopg2.DatabaseError):
            call(pg.db)
    assert message in caplog.text
    pg.pool.putconn.assert_called_with(pg.conn, close=True)


# ── copy_table ────────────────────────────────────────────────────────────


def test_copy_table_commits_in_a_transaction(pg):
    pg.db.copy_table("src", "dst")

    assert [text for text, _ in pg.executed] == [
        'CREATE TABLE IF NOT EXISTS "dst" (LIKE "src" INCLUDING ALL)',
        'INSERT INTO "dst" SELECT * FROM "src"',
    ]
    pg.conn.commit.assert_called_once()
    pg.conn.rollback.assert_not_called()
    assert pg.conn.autocommit is True
    pg.pool.putconn.assert_called_with(pg.conn)


def test_copy_table_rolls_back_on_failure(pg, caplog):
    pg.fail = psycopg2.DatabaseError("disk full")
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        with pytest.raises(psycopg2.DatabaseError):
            pg.db.copy_table("src", "dst")
    pg.conn.rollback.assert_called_once()
    pg.conn.commit.assert_not_called()
    assert pg.conn.autocommit is True
    pg.pool.putconn.assert_called_with(pg.conn)
    assert "Error in copy_table" in caplog.text


def test_copy_table_without_connection(pg):
    pg.pool.getconn.return_value = None
    with pytest.raises(OperationalError, match="Failed to get connection"):
        pg.db.copy_table("src", "dst")


def test_copy_table_validates_identifiers_first(pg):
    with pytest.raises(ValueError, match="destination table"):
        pg.db.copy_table("src", "dst; DROP TABLE users")
    pg.pool.getconn.assert_not_called()


# ── ensure_table ─────────────────────────────────────────────────────────


class InvoiceLine(BaseModel):
    id: str
    amount: int
    note: Optional[str]
    either: Union[int, str]


def test_ensure_table_default_name_and_existing_columns_untouched(pg):
    pg.cur.fetchall.return_value = [
        {"column_name": "id", "data_type": "character varying", "udt_name": "varchar"},
        {"column_name": "amount", "data_type": "bigint", "udt_name": "int8"},
        {"column_name": "note", "data_type": "text", "udt_name": "text"},
        {"column_name": "either", "data_type": "bigint", "udt_name": "int8"},
    ]

    pg.db.ensure_table(InvoiceLine)

    create = pg.executed[0][0]
    assert create == (
        'CREATE TABLE IF NOT EXISTS "invoice_line" ("id" VARCHAR(255) PRIMARY KEY, '
        '"amount" BIGINT NOT NULL, "note" TEXT, "either" BIGINT NOT NULL)'
    )
    assert not any(text.startswith("ALTER") for text, _ in pg.executed)


class _LegacyField(SimpleNamespace):
    pass


class LegacyThing:
    __fields__ = {
        "title": _LegacyField(annotation=str, default=None),
        "count": int,
    }


def test_ensure_table_legacy_fields_shape(pg):
    pg.cur.fetchall.return_value = []
    pg.db.ensure_table(LegacyThing, "legacy_thing")
    assert pg.executed[0][0] == (
        'CREATE TABLE IF NOT EXISTS "legacy_thing" ("id" VARCHAR(255) PRIMARY KEY, '
        '"title" TEXT, "count" BIGINT NOT NULL)'
    )


def test_ensure_table_failure_is_logged_and_raised(pg, caplog):
    pg.fail = psycopg2.DatabaseError("permission denied")
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        with pytest.raises(psycopg2.DatabaseError):
            pg.db.ensure_table(InvoiceLine)
    assert "Error ensuring table for model InvoiceLine" in caplog.text
