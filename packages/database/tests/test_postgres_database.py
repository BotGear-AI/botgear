import os
import pytest
from unittest.mock import patch, MagicMock
from psycopg2 import OperationalError, sql
from database.postgres_database import PostgresDatabase
from models.database_postgres_config import DatabasePostgresConfig

@pytest.fixture
def postgres_db():
    config = DatabasePostgresConfig(
        name="test_postgres",
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", 5432)),
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=os.getenv("POSTGRES_PASSWORD", "postgres"),
        database=os.getenv("POSTGRES_DB", "test_db"),
        sslmode=os.getenv("POSTGRES_SSLMODE", "prefer")
    )
    with patch("database.postgres_database.ThreadedConnectionPool") as mock_pool_class:
        # Create mock pool and connection
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        
        # Setup the pool to return our mock connection
        mock_pool_class.return_value = mock_pool
        mock_pool.getconn.return_value = mock_conn
        
        # Setup the connection to return our mock cursor
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_conn.autocommit = True
        
        # Setup default fetchone/fetchall for all tests
        mock_cursor.fetchone.return_value = {"id": "1", "uuid": "1", "value": "foo"}
        mock_cursor.fetchall.return_value = [
            {"id": "a", "uuid": "a"},
            {"id": "b", "uuid": "b"}
        ]
        
        db = PostgresDatabase(config)
        db._mock_pool = mock_pool
        db._mock_conn = mock_conn
        db._mock_cursor = mock_cursor
        yield db

def test_insert_and_get_item(postgres_db):
    item = {"id": "1", "uuid": "1", "value": "foo"}
    postgres_db._mock_cursor.fetchone.return_value = item
    postgres_db.insert_item("test", "1", item)
    result = postgres_db.get_item("test", "1")
    assert result["id"] == "1"
    assert result["value"] == "foo"


def _render_sql(statement):
    if isinstance(statement, sql.SQL):
        return statement.string
    if isinstance(statement, sql.Identifier):
        return ".".join(f'"{part}"' for part in statement.strings)
    if isinstance(statement, sql.Placeholder):
        return statement._wrapped if statement._wrapped is not None else "%s"
    if isinstance(statement, sql.Composed):
        return "".join(_render_sql(part) for part in statement.seq)
    raise TypeError(f"Unexpected SQL node: {type(statement).__name__}")


def test_prepare_insert_statement_controls_returning_and_serializes_values(postgres_db):
    item = {"id": "1", "paths": ["/docs"]}

    statement, values = postgres_db.prepare_insert_statement("search_spec", item)
    assert _render_sql(statement).endswith("RETURNING *")
    assert values == ["1", '["/docs"]']

    statement, values = postgres_db.prepare_insert_statement(
        "search_spec", item, returning=False
    )
    assert not _render_sql(statement).endswith("RETURNING *")
    assert values == ["1", '["/docs"]']


@pytest.mark.parametrize(
    "item, message",
    [({}, "Cannot insert empty item"), ({"name": "missing id"}, "must contain 'id'")],
)
def test_prepare_insert_statement_validates_item(postgres_db, item, message):
    with pytest.raises(ValueError, match=message):
        postgres_db.prepare_insert_statement("search_spec", item)


def test_successful_transaction_executes_statements_in_order_and_commits(postgres_db):
    with postgres_db.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("FIRST STATEMENT")
            cursor.execute("SECOND STATEMENT")

    executed = [
        call.args[0]
        for call in postgres_db._mock_cursor.execute.call_args_list
        if call.args[0] != "SELECT 1"
    ]
    assert executed == ["FIRST STATEMENT", "SECOND STATEMENT"]
    postgres_db._mock_conn.commit.assert_called_once()
    postgres_db._mock_conn.rollback.assert_not_called()


def test_failed_transaction_rolls_back_after_second_statement(postgres_db):
    def execute_side_effect(statement, *args, **kwargs):
        if statement == "SECOND STATEMENT":
            raise OperationalError("statement failed")

    postgres_db._mock_cursor.execute.side_effect = execute_side_effect

    with pytest.raises(OperationalError, match="statement failed"):
        with postgres_db.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute("FIRST STATEMENT")
                cursor.execute("SECOND STATEMENT")

    executed = [
        call.args[0]
        for call in postgres_db._mock_cursor.execute.call_args_list
        if call.args[0] != "SELECT 1"
    ]
    assert executed == ["FIRST STATEMENT", "SECOND STATEMENT"]
    postgres_db._mock_conn.commit.assert_not_called()
    postgres_db._mock_conn.rollback.assert_called_once()


def test_insert_and_get_item_with_json_fields(postgres_db):
    # Arrays and dicts should be stored as JSONB
    item = {
        "id": "json1",
        "uuid": "json1",
        "tags": ["a", "b", "c"],
        "meta": {"foo": 1, "bar": [1, 2, 3]},
        "value": "foo"
    }
    postgres_db._mock_cursor.fetchone.return_value = item
    postgres_db.insert_item("test", "json1", item)
    result = postgres_db.get_item("test", "json1")
    assert result["tags"] == ["a", "b", "c"]
    assert result["meta"] == {"foo": 1, "bar": [1, 2, 3]}
    assert result["value"] == "foo"

def test_upsert_item(postgres_db):
    # First upsert
    postgres_db._mock_cursor.fetchone.return_value = {"id": "upsert1", "uuid": "upsert1", "name": "First"}
    item = {"id": "upsert1", "uuid": "upsert1", "name": "First"}
    result = postgres_db.upsert_item("test", "upsert1", item)
    assert result["id"] == "upsert1"
    # Update upsert
    postgres_db._mock_cursor.fetchone.return_value = {"id": "upsert1", "name": "Updated"}
    updated = postgres_db.upsert_item("test", "upsert1", {"id": "upsert1", "name": "Updated"})
    assert updated["name"] == "Updated"

def test_delete_item(postgres_db):
    item = {"id": "3", "uuid": "3", "value": "del"}
    postgres_db.insert_item("test", "3", item)
    postgres_db.delete_item("test", "3")
    postgres_db._mock_cursor.fetchone.return_value = None
    assert postgres_db.get_item("test", "3") is None

def test_get_all_items(postgres_db):
    postgres_db.insert_item("test", "a", {"id": "a", "uuid": "a"})
    postgres_db.insert_item("test", "b", {"id": "b", "uuid": "b"})
    postgres_db._mock_cursor.fetchall.return_value = [{"id": "a", "uuid": "a"}, {"id": "b", "uuid": "b"}]
    items = postgres_db.get_all_items("test")
    ids = {item["id"] for item in items}
    assert ids == {"a", "b"}

import datetime
from typing import Optional
from pydantic import BaseModel

def test_boolean_type_inference(postgres_db):
    """Test that boolean values are correctly inferred as BOOLEAN type, not BIGINT"""
    test_item = {
        "id": "bool_test",
        "uuid": "bool_test", 
        "name": "Test User",
        "age": 25,
        "height": 5.9,
        "is_active": True,
        "receive_email_updates": False,
        "preferences": {"theme": "dark"},
        "tags": ["user"]
    }
    
    # Test the _infer_columns method directly
    columns = postgres_db._infer_columns(test_item)
    
    # Verify boolean fields are correctly identified as BOOLEAN, not BIGINT
    assert columns["is_active"] == "BOOLEAN", f"Expected BOOLEAN, got {columns['is_active']}"
    assert columns["receive_email_updates"] == "BOOLEAN", f"Expected BOOLEAN, got {columns['receive_email_updates']}"
    
    # Verify other types are still correct
    assert columns["age"] == "BIGINT", f"Expected BIGINT, got {columns['age']}"
    assert columns["height"] == "DOUBLE PRECISION", f"Expected DOUBLE PRECISION, got {columns['height']}"
    assert columns["name"] == "TEXT", f"Expected TEXT, got {columns['name']}"
    assert columns["preferences"] == "JSONB", f"Expected JSONB, got {columns['preferences']}"
    assert columns["tags"] == "JSONB", f"Expected JSONB, got {columns['tags']}"

def test_datetime_infer_columns(postgres_db):
    """datetime.datetime values infer as TIMESTAMPTZ, not TEXT"""
    now = datetime.datetime(2024, 6, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    item = {
        "id": "dt_test",
        "created_at": now,
        "name": "thing",
    }
    columns = postgres_db._infer_columns(item)
    assert columns["created_at"] == "TIMESTAMPTZ", f"Expected TIMESTAMPTZ, got {columns['created_at']}"
    assert columns["name"] == "TEXT"

def test_datetime_naive_infer_columns(postgres_db):
    """Naive (no tz) datetime.datetime also maps to TIMESTAMPTZ"""
    naive = datetime.datetime(2024, 1, 1, 0, 0, 0)
    columns = postgres_db._infer_columns({"id": "x", "ts": naive})
    assert columns["ts"] == "TIMESTAMPTZ"

def test_get_postgres_type_datetime(postgres_db):
    assert postgres_db._get_postgres_type(datetime.datetime) == "TIMESTAMPTZ"

def test_get_postgres_type_date(postgres_db):
    assert postgres_db._get_postgres_type(datetime.date) == "DATE"

def test_get_postgres_type_time(postgres_db):
    assert postgres_db._get_postgres_type(datetime.time) == "TIMETZ"

def test_get_postgres_type_optional_datetime(postgres_db):
    """Optional[datetime.datetime] unwraps to TIMESTAMPTZ"""
    from typing import Optional
    assert postgres_db._get_postgres_type(Optional[datetime.datetime]) == "TIMESTAMPTZ"

def test_prepare_values_datetime_passthrough(postgres_db):
    """datetime values are passed through as-is (psycopg2 handles adaptation)"""
    now = datetime.datetime(2024, 6, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    item = {"id": "x", "created_at": now, "name": "foo"}
    values = postgres_db._prepare_values(item)
    assert values[1] is now  # not serialized to string or json
    assert isinstance(values[1], datetime.datetime)

def test_ensure_table_with_datetime_model(postgres_db):
    """ensure_table generates TIMESTAMPTZ for datetime fields in a Pydantic model"""
    class EventModel(BaseModel):
        id: str
        name: str
        occurred_at: datetime.datetime
        scheduled_for: Optional[datetime.datetime] = None

    captured_sql = []
    postgres_db._mock_cursor.execute.side_effect = lambda sql, *a, **kw: captured_sql.append(sql)
    postgres_db._mock_cursor.fetchall.return_value = []

    postgres_db.ensure_table(EventModel, "event_model")

    assert captured_sql, "No SQL was executed"
    # _get_cursor validates a pooled connection with a plain "SELECT 1" before
    # yielding it (see _acquire_validated_connection) — skip that, not the DDL.
    ddl = next(s for s in captured_sql if s != "SELECT 1")

    # DDL is a psycopg2.sql.Composed; flatten Identifier+SQL children into a
    # single string so we can assert on the structure without needing a live
    # connection (sql.as_string requires one).
    from psycopg2 import sql as _pgsql
    def _flatten(node) -> str:
        if isinstance(node, _pgsql.Identifier):
            return '"' + '"."'.join(node.strings) + '"'
        if isinstance(node, _pgsql.SQL):
            return node.string
        if isinstance(node, _pgsql.Composed):
            return "".join(_flatten(c) for c in node.seq)
        return str(node)

    rendered = _flatten(ddl)
    assert "TIMESTAMPTZ" in rendered, f"Expected TIMESTAMPTZ in DDL:\n{rendered}"
    assert '"occurred_at" TIMESTAMPTZ NOT NULL' in rendered
    assert '"scheduled_for" TIMESTAMPTZ' in rendered
    # scheduled_for is Optional so should not have NOT NULL
    assert '"scheduled_for" TIMESTAMPTZ NOT NULL' not in rendered


def test_ensure_table_widens_existing_integer_columns_to_bigint(postgres_db):
    class LegacyJobModel(BaseModel):
        id: str
        created: int

    captured_sql = []

    def execute(sql, *args, **kwargs):
        captured_sql.append(sql)

    postgres_db._mock_cursor.execute.side_effect = execute
    postgres_db._mock_cursor.fetchall.return_value = [
        {"column_name": "id", "data_type": "character varying", "udt_name": "varchar"},
        {"column_name": "created", "data_type": "integer", "udt_name": "int4"},
    ]

    postgres_db.ensure_table(LegacyJobModel, "legacy_job")

    # SQL is now a psycopg2.sql.Composed — flatten Identifier+SQL children
    # into plain text so the assertion can match the rendered DDL.
    from psycopg2 import sql as _pgsql
    def _flatten(node) -> str:
        if isinstance(node, _pgsql.Identifier):
            return '"' + '"."'.join(node.strings) + '"'
        if isinstance(node, _pgsql.SQL):
            return node.string
        if isinstance(node, _pgsql.Composed):
            return "".join(_flatten(c) for c in node.seq)
        return str(node)

    rendered = [_flatten(s) for s in captured_sql]
    assert any(
        'ALTER TABLE "legacy_job" ALTER COLUMN "created" TYPE BIGINT USING "created"::BIGINT' in s
        for s in rendered
    ), f"Expected BIGINT widening SQL, got: {rendered}"


def test_query_items_rejects_sql_injection_field_name(postgres_db):
    """query_items should reject unsafe dynamic column names before SQL execution."""
    with pytest.raises(ValueError, match="field name"):
        postgres_db.query_items("test", {'id" OR 1=1 --': "abc"})

    postgres_db._mock_cursor.execute.assert_not_called()


def test_query_logs_rendered_sql_when_debug_enabled(postgres_db):
    postgres_db._mock_cursor.mogrify.return_value = b'SELECT * FROM "test" WHERE id = \'1\''

    with patch("database.postgres_database.logger.isEnabledFor", return_value=True), patch(
        "database.postgres_database.logger.debug"
    ) as mock_debug:
        postgres_db.query('SELECT * FROM "test" WHERE id = %s', ("1",))

    assert any(
        call.args == ("SQL: %s", 'SELECT * FROM "test" WHERE id = \'1\'')
        for call in mock_debug.call_args_list
    )


# --- query_readonly ------------------------------------------------------------
# Ad-hoc admin/debug query path — must never touch the app's normal
# read/write pool since it forces the connection into a read-only session
# and a custom statement_timeout that must persist beyond a single
# cursor.execute() (autocommit=True on every pooled connection means those
# settings need to be session-level, not transaction-scoped).

def test_query_readonly_sets_session_readonly_and_timeout(postgres_db):
    postgres_db.query_readonly('SELECT 1', timeout_ms=5000)

    postgres_db._mock_conn.set_session.assert_called_once_with(readonly=True)
    executed = [c.args[0] for c in postgres_db._mock_cursor.execute.call_args_list]
    assert any("SET statement_timeout = 5000" in stmt for stmt in executed)


def test_query_readonly_discards_connection_never_returns_to_pool(postgres_db):
    postgres_db.query_readonly('SELECT 1')

    postgres_db._mock_pool.putconn.assert_called_once_with(postgres_db._mock_conn, close=True)


def test_query_readonly_discards_connection_even_on_query_error(postgres_db):
    postgres_db._mock_cursor.execute.side_effect = [None, Exception("boom")]

    with pytest.raises(Exception, match="boom"):
        postgres_db.query_readonly('SELECT 1')

    postgres_db._mock_pool.putconn.assert_called_once_with(postgres_db._mock_conn, close=True)


def test_query_readonly_truncates_at_row_limit(postgres_db):
    postgres_db._mock_cursor.fetchmany.return_value = [{"id": str(i)} for i in range(3)]

    rows, truncated = postgres_db.query_readonly('SELECT 1', row_limit=2)

    assert len(rows) == 2
    assert truncated is True
    postgres_db._mock_cursor.fetchmany.assert_called_once_with(3)  # row_limit + 1


def test_query_readonly_not_truncated_when_under_limit(postgres_db):
    postgres_db._mock_cursor.fetchmany.return_value = [{"id": "1"}]

    rows, truncated = postgres_db.query_readonly('SELECT 1', row_limit=500)

    assert len(rows) == 1
    assert truncated is False


def test_query_readonly_rejects_empty_sql(postgres_db):
    with pytest.raises(ValueError):
        postgres_db.query_readonly("   ")

    postgres_db._mock_cursor.execute.assert_not_called()


# --- _get_cursor retry behavior -----------------------------------------------
# Regression tests for the "generator didn't stop after throw()" bug: the old
# implementation retried the *entire* yield-body (including the caller's own
# query) inside the same @contextmanager generator, which Python disallows —
# a caller-side failure crashed with that RuntimeError instead of surfacing
# the real error. The fix scopes retry to connection *acquisition* only
# (_acquire_validated_connection, pinged with SELECT 1 before being handed to
# the caller); a failure inside the caller's own `with` block always
# propagates directly, on the first and only attempt.

def _make_conn():
    conn = MagicMock()
    conn.autocommit = True
    return conn


def test_get_cursor_retries_a_stale_pooled_connection(postgres_db):
    """A connection that fails its pre-flight SELECT 1 ping (the classic
    "went stale while idle in the pool" symptom) is discarded and a fresh one
    is acquired — transparently, before the caller ever sees it."""
    stale_conn = _make_conn()
    stale_ping_cursor = MagicMock()
    stale_ping_cursor.execute.side_effect = OperationalError("SSL connection has been closed unexpectedly")
    stale_conn.cursor.return_value.__enter__.return_value = stale_ping_cursor

    fresh_conn = _make_conn()
    fresh_cursor = MagicMock()
    fresh_conn.cursor.return_value.__enter__.return_value = fresh_cursor

    postgres_db._mock_pool.getconn.side_effect = [stale_conn, fresh_conn]

    with patch("database.postgres_database.time.sleep"):
        with postgres_db._get_cursor() as cursor:
            assert cursor is fresh_cursor

    postgres_db._mock_pool.putconn.assert_any_call(stale_conn, close=True)
    postgres_db._mock_pool.putconn.assert_any_call(fresh_conn)


def test_get_cursor_propagates_caller_query_error_without_retry(postgres_db):
    """A failure inside the caller's own query (i.e. after the cursor was
    already handed over) must not be retried — only the real error is
    surfaced, never the generator RuntimeError, and getconn is called
    exactly once (no re-acquisition, no re-running the caller's code)."""
    # The fixture's mock_conn.cursor() returns the same mock_cursor for both
    # the pre-flight ping and the caller's own use, so let the ping (the
    # first execute call) succeed and only fail the caller's own query.
    calls = {"n": 0}

    def execute_side_effect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OperationalError("SSL connection has been closed unexpectedly")

    postgres_db._mock_cursor.execute.side_effect = execute_side_effect

    with pytest.raises(OperationalError, match="SSL connection has been closed unexpectedly"):
        with postgres_db._get_cursor() as cursor:
            cursor.execute("SELECT * FROM real_query")

    assert postgres_db._mock_pool.getconn.call_count == 1
    postgres_db._mock_pool.putconn.assert_called_once_with(postgres_db._mock_conn, close=True)


def test_get_cursor_raises_real_error_after_exhausting_acquisition_retries(postgres_db):
    """If every pooled connection is stale, the real OperationalError is
    raised after 3 attempts — not swallowed, not turned into a RuntimeError."""
    bad_conn = _make_conn()
    bad_ping_cursor = MagicMock()
    bad_ping_cursor.execute.side_effect = OperationalError("SSL connection has been closed unexpectedly")
    bad_conn.cursor.return_value.__enter__.return_value = bad_ping_cursor
    postgres_db._mock_pool.getconn.return_value = bad_conn

    with patch("database.postgres_database.time.sleep"):
        with pytest.raises(OperationalError, match="SSL connection has been closed unexpectedly"):
            with postgres_db._get_cursor():
                pass  # pragma: no cover - never reached

    assert postgres_db._mock_pool.getconn.call_count == 3


def test_discard_connection_closes_conn_when_putconn_itself_fails(postgres_db):
    """If putconn(close=True) raises (e.g. the pool is already being torn
    down), the bad connection must still be closed directly — mirroring
    _return_connection's own fallback — so the socket/DB session isn't
    leaked while the pool is rebuilt."""
    bad_conn = MagicMock()
    postgres_db._mock_pool.putconn.side_effect = Exception("pool is closed")

    with patch("database.postgres_database.ThreadedConnectionPool"):
        postgres_db._discard_connection(bad_conn)

    bad_conn.close.assert_called_once()


def test_acquire_validated_connection_does_not_leak_on_non_retriable_ping_error(postgres_db):
    """A connection successfully checked out via getconn() must always be
    returned to the pool, even when the SELECT 1 ping fails with something
    outside the retried (OperationalError/InterfaceError/PoolError) set —
    e.g. a plain ValueError, or a psycopg2.Error subtype like DataError that
    isn't one of those three. Not retried (not known to be transient), but
    the checkout slot must not leak."""
    postgres_db._mock_conn.cursor.return_value.__enter__.return_value.execute.side_effect = ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        postgres_db._acquire_validated_connection()

    postgres_db._mock_pool.getconn.assert_called_once()
    postgres_db._mock_pool.putconn.assert_called_once_with(postgres_db._mock_conn, close=True)
