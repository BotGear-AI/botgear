import logging
import datetime
from typing import Dict, List, Any, Optional, Tuple, Union
import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor, register_default_jsonb
from psycopg2 import OperationalError, InterfaceError
from psycopg2.pool import ThreadedConnectionPool, PoolError
import threading
import time
import json
import re
from contextlib import contextmanager
from .interface import DatabaseAdapter
from models.database_postgres_config import DatabasePostgresConfig
from pydantic import BaseModel

logger = logging.getLogger(__name__)

class PostgresDatabase(DatabaseAdapter):
    def __init__(self, config: DatabasePostgresConfig):
        self.config = config
        self.sslmode = config.sslmode if config.sslmode not in (None, "None", "") else "prefer"
        self._pool = None
        self._lock = threading.Lock()
        self._connect()
        logger.info(f"PostgresDatabase initialized with connection pool (min={config.min_connections}, max={config.max_connections}) at {config.host}:{config.port}/{config.database}")

    def _connect(self) -> None:
        """Create a connection pool"""
        try:
            if self._pool:
                try:
                    self._pool.closeall()
                except Exception as e:
                    logger.warning(f"Error closing existing pool: {e}")
            
            self._pool = ThreadedConnectionPool(
                minconn=self.config.min_connections,
                maxconn=self.config.max_connections,
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                dbname=self.config.database,
                sslmode=self.sslmode,
                connect_timeout=10,
                # Enable TCP keepalives to detect dead connections
                keepalives_idle=600,
                keepalives_interval=30,
                keepalives_count=3
            )
            register_default_jsonb(globally=True, loads=json.loads)
            logger.info(f"Database connection pool established with {self.config.min_connections}-{self.config.max_connections} connections")
        except Exception as e:
            logger.error(f"Failed to create connection pool: {e}")
            self._pool = None
            raise

    def _discard_connection(self, conn) -> None:
        """Return a bad/unvalidated connection to the pool for closing —
        never reuse it. If even returning it fails, rebuild the pool so the
        checkout slot isn't leaked. None-safe (no-op if a connection was
        never actually acquired)."""
        if conn is None:
            return
        try:
            self._pool.putconn(conn, close=True)
        except Exception as put_error:
            logger.error(f"Error returning bad connection to pool: {put_error}")
            try:
                conn.close()
            except Exception:
                pass
            try:
                with self._lock:
                    self._connect()
            except Exception as reconnect_error:
                logger.error(f"Error rebuilding pool after bad-connection return failure: {reconnect_error}")

    def _return_connection(self, conn) -> None:
        """Return a healthy connection to the pool."""
        try:
            self._pool.putconn(conn)
        except Exception as put_error:
            logger.error(f"Error returning connection to pool: {put_error}")
            try:
                conn.close()
            except Exception:
                pass
            try:
                with self._lock:
                    self._connect()
            except Exception as reconnect_error:
                logger.error(f"Error rebuilding pool after return failure: {reconnect_error}")

    def _acquire_validated_connection(self):
        """Get a connection from the pool, retrying if a pooled connection turns
        out to be dead (e.g. it went stale while idle in the pool, surfacing as
        "SSL connection has been closed unexpectedly" on first use — the common
        real-world trigger here). Validated with a cheap SELECT 1 before being
        handed back, so a stale connection is caught here and discarded/retried
        rather than failing inside the caller's own query.

        Retrying is intentionally scoped to *this* step only — see _get_cursor's
        docstring for why the retry loop cannot also cover the caller's query.

        Any connection successfully obtained via getconn() is *always*
        returned to the pool (discarded on failure) before this method
        returns or raises — including on an exception type outside the
        retried set below (e.g. a non-retriable psycopg2 error from the
        ping) — so a checked-out connection is never leaked.
        """
        max_retries = 3
        for attempt in range(max_retries):
            conn = None
            try:
                conn = self._pool.getconn()
                if not conn:
                    raise OperationalError("Failed to get connection from pool")
                conn.autocommit = True
                with conn.cursor() as ping_cursor:
                    ping_cursor.execute("SELECT 1")
                return conn
            except (OperationalError, InterfaceError, PoolError) as e:
                logger.warning(f"Database operation failed (attempt {attempt + 1}/{max_retries}): {e}")
                self._discard_connection(conn)

                if isinstance(e, PoolError) and "exhausted" in str(e).lower():
                    logger.warning("Connection pool exhausted; rebuilding pool before retry")
                    try:
                        with self._lock:
                            self._connect()
                    except Exception as reconnect_error:
                        logger.error(f"Failed to rebuild exhausted pool: {reconnect_error}")

                if attempt == max_retries - 1:
                    logger.error(f"All {max_retries} retry attempts failed")
                    raise

                # Exponential backoff before retry
                time.sleep(0.5 * (attempt + 1))
            except Exception:
                # Not one of the known-transient/retriable error types — but
                # a connection may still have been successfully checked out
                # from the pool (e.g. getconn() succeeded and the ping
                # raised something else). Discard it so the slot isn't
                # leaked, then propagate: this class of failure isn't known
                # to be safely retriable, so it's surfaced immediately
                # rather than silently retried.
                self._discard_connection(conn)
                raise

    @contextmanager
    def _get_cursor(self, cursor_factory=None):
        """Context manager for database cursors with connection pooling and
        automatic retry of connection acquisition.

        Retry is scoped to getting a *validated* connection (see
        _acquire_validated_connection) — it cannot also cover the caller's own
        query inside the `with` block. A @contextmanager generator may only
        yield once per invocation: if the caller's block raises, Python
        resumes this generator by throwing that exception in at the `yield`
        below, and if the generator then looped back and yielded *again*
        instead of stopping, contextlib itself raises `RuntimeError:
        generator didn't stop after throw()` — which is exactly the bug this
        replaced (a previous version retried the whole yield-body on
        failure, including the caller's query, and crashed with that
        RuntimeError instead of surfacing the real error). A query that
        fails after the cursor was already handed to the caller propagates
        directly; retrying arbitrary caller code (which may have already had
        partial side effects, e.g. a write) would not be safe to do
        transparently anyway.
        """
        if not self._pool:
            logger.error("Connection pool not initialized")
            raise OperationalError("Database connection pool is not available")

        conn = self._acquire_validated_connection()
        cursor_error = False
        try:
            with conn.cursor(cursor_factory=cursor_factory) as cursor:
                yield cursor
                # Success - cursor is now closed, safe to return connection
        except Exception:
            # Mark that an error occurred during cursor operations
            cursor_error = True
            raise
        finally:
            # Always return connection to pool after cursor is closed
            if cursor_error:
                self._discard_connection(conn)
            else:
                self._return_connection(conn)

    def _rollback_transaction(self, conn) -> bool:
        """Roll back a transaction and report whether the connection is reusable."""
        try:
            conn.rollback()
        except BaseException as rollback_error:
            logger.warning(f"Transaction rollback failed: {rollback_error}")
            return False
        return True

    def _restore_autocommit(self, conn) -> None:
        conn.autocommit = True

    @contextmanager
    def transaction(self):
        """Yield one connection and commit or roll back its statements atomically.

        Only statements executed through the yielded connection are part of the
        transaction. Database helpers called inside this block acquire their own
        pooled connection and are not included.

        Example:
            with db.transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("UPDATE users SET status = %s WHERE id = %s", ("active", user_id))
                    cursor.execute("INSERT INTO audit_log (user_id) VALUES (%s)", (user_id,))
        """
        if not self._pool:
            raise OperationalError("Database connection pool is not available")

        conn = self._acquire_validated_connection()
        connection_returned = False
        try:
            conn.autocommit = False
            try:
                yield conn
            except BaseException as transaction_error:
                # Body failures are reusable when rollback succeeds.
                if self._rollback_transaction(conn):
                    try:
                        self._restore_autocommit(conn)
                    except BaseException as reset_error:
                        logger.warning(f"Failed to reset autocommit: {reset_error}")
                        raise transaction_error
                    self._return_connection(conn)
                    connection_returned = True
                raise
            else:
                # A commit failure leaves the transaction outcome uncertain.
                try:
                    conn.commit()
                except BaseException:
                    self._rollback_transaction(conn)
                    raise
                self._restore_autocommit(conn)
        except BaseException:
            if not connection_returned:
                self._discard_connection(conn)
            raise
        else:
            self._return_connection(conn)

    def close(self) -> None:
        """Clean shutdown of database connection pool"""
        with self._lock:
            if self._pool:
                try:
                    self._pool.closeall()
                    logger.info("Database connection pool closed")
                except Exception as e:
                    logger.error(f"Error closing database connection pool: {e}")
                finally:
                    self._pool = None

    def _render_sql_for_logging(self, cur, statement: Any, params: Optional[Union[list, tuple]] = None) -> Optional[str]:
        """Render SQL using psycopg2 quoting for debug logs."""
        if not logger.isEnabledFor(logging.DEBUG):
            return None

        try:
            rendered_statement = statement.as_string(cur) if isinstance(statement, sql.Composable) else statement
            if params is None:
                return rendered_statement
            return cur.mogrify(rendered_statement, params).decode("utf-8")
        except Exception as exc:
            logger.debug("Unable to render SQL for logging: %s", exc)
            return None

    def _execute(self, cur, statement: Any, params: Optional[Union[list, tuple]] = None) -> None:
        rendered_sql = self._render_sql_for_logging(cur, statement, params)
        if rendered_sql:
            logger.debug("SQL: %s", rendered_sql)

        if params is None:
            cur.execute(statement)
        else:
            cur.execute(statement, params)

    def _infer_columns(self, item: dict) -> Dict[str, str]:
        """Infer PostgreSQL column types from Python values - use TEXT for mixed types"""
        columns = {}
        for k, v in item.items():
            if v is None:
                columns[k] = "TEXT"
            elif isinstance(v, bool):
                columns[k] = "BOOLEAN"
            elif isinstance(v, list):
                # Always use JSONB for lists (do NOT use TEXT[])
                columns[k] = "JSONB"
            elif isinstance(v, dict):
                columns[k] = "JSONB"
            elif isinstance(v, datetime.datetime):
                columns[k] = "TIMESTAMPTZ"
            elif isinstance(v, float):
                columns[k] = "DOUBLE PRECISION"
            elif isinstance(v, int):
                if k.lower() in ['zip', 'zipcode', 'postal_code', 'phone', 'id', 'code', 'number', 'parcel_id', 'parcel_number', 'raw_parcel_number', 'cs', 'owner_zip']:
                    columns[k] = "TEXT"
                else:
                    columns[k] = "BIGINT"
            else:
                columns[k] = "TEXT"
        return columns

    def _prepare_values(self, item: dict) -> List:
        """Convert Python values to PostgreSQL-compatible format"""
        values = []
        try:
            for k, v in item.items():
                if isinstance(v, (list, dict)):
                    # Serialize lists and dicts to JSON for jsonb columns.
                    # default=str coerces non-JSON-native values that ride
                    # along in free-form JSONB blobs (datetime, Decimal,
                    # UUID, etc.) to their string form rather than raising
                    # TypeError. Triggered in practice by openpyxl date
                    # cells landing in raw_sheet_row.data.
                    values.append(json.dumps(v, default=str))
                else:
                    values.append(v)
            return values
        except Exception as e:
            logger.error(f"Error in _prepare_values with item keys {list(item.keys())}: {e}")
            logger.error(f"Item: {item}")
            raise

    def upsert_item(self, table: str, key: str, item: dict) -> Optional[dict]:
        """Upsert item with validation"""
        self._validate_identifier(table, "table name")
        for k in item.keys():
            self._validate_identifier(k, f"column '{k}'")

        if not item:
            raise ValueError("Cannot upsert empty item")
        if 'id' not in item:
            raise ValueError("Item must contain 'id' field")

        try:
            values = self._prepare_values(item)
            if len(values) != len(item):
                raise ValueError(f"Values count ({len(values)}) doesn't match item keys count ({len(item)})")

            stmt = sql.SQL(
                "INSERT INTO {table} ({cols}) VALUES ({vals}) "
                "ON CONFLICT (id) DO UPDATE SET {upd} RETURNING *"
            ).format(
                table=sql.Identifier(table),
                cols=sql.SQL(", ").join(sql.Identifier(k) for k in item.keys()),
                vals=sql.SQL(", ").join(sql.Placeholder() for _ in item),
                upd=sql.SQL(", ").join(
                    sql.SQL("{c}=EXCLUDED.{c}").format(c=sql.Identifier(k))
                    for k in item.keys()
                ),
            )

            with self._get_cursor(cursor_factory=RealDictCursor) as cur:
                self._execute(cur, stmt, values)
                result = cur.fetchone()
                return dict(result) if result else None
        except Exception as e:
            logger.error(f"Error in upsert_item for table '{table}', key '{key}': {e}")
            logger.error(f"Item keys: {list(item.keys())}")
            raise

    def insert_item(self, table: str, key: str, item: dict) -> Optional[dict]:
        """Insert item with validation"""
        self._validate_identifier(table, "table name")
        for k in item.keys():
            self._validate_identifier(k, f"column '{k}'")

        if not item:
            raise ValueError("Cannot insert empty item")
        if 'id' not in item:
            raise ValueError("Item must contain 'id' field")

        try:
            values = self._prepare_values(item)
            stmt = sql.SQL(
                "INSERT INTO {table} ({cols}) VALUES ({vals}) RETURNING *"
            ).format(
                table=sql.Identifier(table),
                cols=sql.SQL(", ").join(sql.Identifier(k) for k in item.keys()),
                vals=sql.SQL(", ").join(sql.Placeholder() for _ in item),
            )

            with self._get_cursor(cursor_factory=RealDictCursor) as cur:
                self._execute(cur, stmt, values)
                result = cur.fetchone()
                return dict(result) if result else None
        except Exception as e:
            logger.error(f"Error in insert_item for table '{table}', key '{key}': {e}")
            logger.error(f"Item keys: {list(item.keys())}")
            raise

    def get_item(self, table: str, key: str) -> Optional[dict]:
        """Get item with validation"""
        self._validate_identifier(table, "table name")

        try:
            with self._get_cursor(cursor_factory=RealDictCursor) as cur:
                stmt = sql.SQL("SELECT * FROM {table} WHERE id = %s").format(
                    table=sql.Identifier(table),
                )
                # External numeric IDs also live in text primary-key columns.
                # Bind as a string so PostgreSQL uses the column type; leave
                # string IDs (including leading zeros) and returned IDs intact.
                lookup_key = str(key) if type(key) is int else key
                self._execute(cur, stmt, (lookup_key,))
                result = cur.fetchone()
                return dict(result) if result else None
        except Exception as e:
            logger.error(f"Error in get_item: {e}")
            raise

    def get_all_items(self, table: str) -> List[dict]:
        """Get all items with validation"""
        self._validate_identifier(table, "table name")

        try:
            with self._get_cursor(cursor_factory=RealDictCursor) as cur:
                stmt = sql.SQL("SELECT * FROM {table}").format(
                    table=sql.Identifier(table),
                )
                self._execute(cur, stmt)
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"Error in get_all_items: {e}")
            raise

    def delete_item(self, table: str, key: str) -> None:
        """Delete item with validation"""
        self._validate_identifier(table, "table name")

        try:
            with self._get_cursor() as cur:
                stmt = sql.SQL("DELETE FROM {table} WHERE id = %s").format(
                    table=sql.Identifier(table),
                )
                self._execute(cur, stmt, (key,))
        except Exception as e:
            logger.error(f"Error in delete_item: {e}")
            raise

    def update_item(self, table: str, key: str, updates: dict, include_nulls: bool = False) -> dict:
        """Update item with validation"""
        self._validate_identifier(table, "table name")

        try:
            # Never allow callers to modify the primary key; rely on `key`.
            filtered_updates = {k: v for k, v in updates.items() if k != "id"}
            if not include_nulls:
                filtered_updates = {k: v for k, v in filtered_updates.items() if v is not None}
            if not filtered_updates:
                logger.warning("update_item called with no updatable fields")
                return {}

            for k in filtered_updates.keys():
                self._validate_identifier(k, f"column '{k}'")

            values = self._prepare_values(filtered_updates)
            stmt = sql.SQL("UPDATE {table} SET {assignments} WHERE id = %s RETURNING *").format(
                table=sql.Identifier(table),
                assignments=sql.SQL(", ").join(
                    sql.SQL("{c} = %s").format(c=sql.Identifier(k))
                    for k in filtered_updates.keys()
                ),
            )

            with self._get_cursor(cursor_factory=RealDictCursor) as cur:
                self._execute(cur, stmt, values + [key])
                result = cur.fetchone()
                return dict(result) if result else {}
        except Exception as e:
            logger.error(f"Error in update_item: {e}")
            raise

    def compare_and_update_item(self, table: str, key: str, expected: dict, updates: dict) -> bool:
        self._validate_identifier(table, "table name")
        if not expected or not updates or "id" in updates:
            raise ValueError("Expected fields and updates are required; id cannot be updated")
        for column in (*expected, *updates):
            self._validate_identifier(column, "column name")
        statement = sql.SQL(
            "UPDATE {table} SET {assignments} WHERE id = %s AND {conditions} RETURNING id"
        ).format(
            table=sql.Identifier(table),
            assignments=sql.SQL(", ").join(
                sql.SQL("{} = %s").format(sql.Identifier(column)) for column in updates
            ),
            conditions=sql.SQL(" AND ").join(
                sql.SQL("{} IS NOT DISTINCT FROM %s").format(sql.Identifier(column)) for column in expected
            ),
        )
        values = self._prepare_values(updates) + [key] + self._prepare_values(expected)
        with self._get_cursor() as cursor:
            self._execute(cursor, statement, values)
            return cursor.fetchone() is not None

    def bulk_update_items(
        self, table: str, updates_by_id: Dict[str, dict], include_nulls: bool = False
    ) -> List[dict]:
        """Multi-row UPDATE ... FROM (VALUES ...) override of the base
        per-row loop — collapses N sequential update_item() round-trips
        (each paying its own connection-pool checkout + validation ping,
        confirmed ~53s for 976 rows in practice) into a handful of
        multi-row statements.

        Rows are grouped by their exact set of update keys (after None-
        filtering, matching update_item's own semantics) so a batch that
        happens to write different columns per row still gets correct
        results — the common case (every row in the batch sets the same
        columns, e.g. a criteria re-evaluation pass) collapses to one
        group. Each group is chunked at 500 rows per statement; well
        under Postgres's bound-parameter ceiling even with several
        columns per row, and keeps any single statement's size bounded
        regardless of how large a caller's batch gets.
        """
        self._validate_identifier(table, "table name")
        if not updates_by_id:
            return []

        groups: Dict[Tuple[str, ...], List[Tuple[str, dict]]] = {}
        for row_id, updates in updates_by_id.items():
            filtered = {k: v for k, v in updates.items() if k != "id"}
            if not include_nulls:
                filtered = {k: v for k, v in filtered.items() if v is not None}
            if not filtered:
                continue
            key_set = tuple(sorted(filtered.keys()))
            groups.setdefault(key_set, []).append((row_id, filtered))

        if not groups:
            logger.warning("bulk_update_items called with no updatable fields")
            return []

        results: List[dict] = []
        chunk_size = 500
        try:
            with self._get_cursor(cursor_factory=RealDictCursor) as cur:
                for key_set, rows in groups.items():
                    for k in key_set:
                        self._validate_identifier(k, f"column '{k}'")
                    # Column types inferred from one representative row's
                    # RAW (pre-serialization) values — same logic
                    # _infer_columns uses elsewhere — so each VALUES
                    # column gets an explicit ::type cast. Needed because
                    # a bare parameter's inferred type in a multi-row
                    # VALUES list doesn't reliably match the target
                    # column (confirmed live: an uncast text param
                    # assigned to a jsonb column fails without this).
                    pg_types = self._infer_columns({k: rows[0][1][k] for k in key_set})

                    for i in range(0, len(rows), chunk_size):
                        chunk = rows[i:i + chunk_size]
                        value_rows = []
                        params: List[Any] = []
                        for row_id, updates in chunk:
                            placeholders = ["%s::text"] + [f"%s::{pg_types[k]}" for k in key_set]
                            value_rows.append("(" + ", ".join(placeholders) + ")")
                            params.append(row_id)
                            params.extend(self._prepare_values({k: updates[k] for k in key_set}))

                        stmt = sql.SQL(
                            "UPDATE {table} AS t SET {assignments} "
                            "FROM (VALUES {value_rows}) AS v(id, {cols}) "
                            "WHERE t.id = v.id RETURNING t.*"
                        ).format(
                            table=sql.Identifier(table),
                            assignments=sql.SQL(", ").join(
                                sql.SQL("{c} = v.{c}").format(c=sql.Identifier(k)) for k in key_set
                            ),
                            value_rows=sql.SQL(", ").join(sql.SQL(vr) for vr in value_rows),
                            cols=sql.SQL(", ").join(sql.Identifier(k) for k in key_set),
                        )
                        self._execute(cur, stmt, params)
                        results.extend(dict(r) for r in cur.fetchall())
            return results
        except Exception as e:
            logger.error(f"Error in bulk_update_items: {e}")
            raise

    def commit_intake_grouping_plan(
        self,
        session_id: str,
        expected_session_metadata: dict,
        session_metadata: dict,
        image_metadata_updates: Dict[str, Dict[str, dict]],
    ) -> None:
        """Commit one reviewed grouping under session and image row locks."""
        conn = None
        try:
            conn = self._acquire_validated_connection()
            if not conn:
                raise OperationalError("Failed to get connection from pool")
            conn.autocommit = False
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    self._execute(
                        cur,
                        'SELECT "metadata" FROM "intake_session" WHERE "id" = %s FOR UPDATE',
                        (session_id,),
                    )
                    session_row = cur.fetchone()
                    if not session_row:
                        raise RuntimeError("grouping session disappeared before commit")
                    if (session_row.get("metadata") or {}) != expected_session_metadata:
                        raise RuntimeError("grouping session changed before commit")

                    image_ids = list(image_metadata_updates)
                    current_images: Dict[str, dict] = {}
                    if image_ids:
                        self._execute(
                            cur,
                            'SELECT "id", "metadata" FROM "intake_image" '
                            'WHERE "intake_session_id" = %s AND "id"::text = ANY(%s) FOR UPDATE',
                            (session_id, image_ids),
                        )
                        current_images = {
                            str(row["id"]): row.get("metadata") or {}
                            for row in cur.fetchall()
                        }
                    if set(current_images) != set(image_ids):
                        raise RuntimeError("grouping photo set changed before commit")
                    for image_id, update in image_metadata_updates.items():
                        if current_images[image_id] != update["expected"]:
                            raise RuntimeError(f"grouping photo {image_id} changed before commit")

                    for image_id, update in image_metadata_updates.items():
                        self._execute(
                            cur,
                            'UPDATE "intake_image" SET "metadata" = %s '
                            'WHERE "intake_session_id" = %s AND "id"::text = %s',
                            (json.dumps(update["new"], default=str), session_id, image_id),
                        )
                        if cur.rowcount != 1:
                            raise RuntimeError(f"grouping photo {image_id} failed to update")
                    self._execute(
                        cur,
                        'UPDATE "intake_session" SET "metadata" = %s WHERE "id" = %s',
                        (json.dumps(session_metadata, default=str), session_id),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError("grouping session failed to update")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True
                self._pool.putconn(conn)
                conn = None
        except Exception as e:
            if conn is not None:
                try:
                    self._pool.putconn(conn, close=True)
                except Exception:
                    pass
            logger.error("Atomic intake grouping commit failed: %s", e)
            raise

    def search_by_key_part(self, table: str, key_part: str) -> List[dict]:
        """Search by key part with validation"""
        self._validate_identifier(table, "table name")

        try:
            with self._get_cursor(cursor_factory=RealDictCursor) as cur:
                stmt = sql.SQL("SELECT * FROM {table} WHERE id LIKE %s").format(
                    table=sql.Identifier(table),
                )
                self._execute(cur, stmt, (f"%{key_part}%",))
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"Error in search_by_key_part: {e}")
            raise

    def query_items(self, table: str, query: dict) -> List[dict]:
        """Query items with validation"""
        self._validate_identifier(table, "table name")
        
        try:
            # Validate dynamic column identifiers before building SQL.
            for field_name in query.keys():
                self._validate_identifier(field_name, f"field name '{field_name}'")

            with self._get_cursor(cursor_factory=RealDictCursor) as cur:
                base_query = sql.SQL("SELECT * FROM {}")
                statement = base_query.format(sql.Identifier(table))

                params: List[Any] = []
                if query:
                    conditions = [
                        sql.SQL("{} = %s").format(sql.Identifier(field_name))
                        for field_name in query.keys()
                    ]
                    statement = sql.SQL("{} WHERE {}").format(
                        statement,
                        sql.SQL(" AND ").join(conditions)
                    )
                    params = list(query.values())

                self._execute(cur, statement, params or None)
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"Error in query_items: {e}")
            raise

    def query_page(
        self,
        table: str,
        *,
        filters: Optional[Dict[str, Any]] = None,
        order_by: Optional[str] = None,
        descending: bool = False,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> Tuple[List[dict], int]:
        """Push ORDER BY / LIMIT / OFFSET / COUNT down into Postgres.

        Identifier safety mirrors query_items: the table, every filter column,
        and order_by are validated and rendered via psycopg2 ``sql.Identifier``
        (never f-string interpolated); the sort direction comes from a fixed
        ASC/DESC branch (a keyword can't be a bound param). Values (filters,
        limit, offset) are always bound parameters.
        """
        self._validate_identifier(table, "table name")
        filters = filters or {}
        for field_name in filters:
            self._validate_identifier(field_name, f"field name '{field_name}'")
        if order_by is not None:
            self._validate_identifier(order_by, "order_by column")

        where = sql.SQL("")
        where_params: List[Any] = []
        if filters:
            conditions = [
                sql.SQL("{} = %s").format(sql.Identifier(field_name))
                for field_name in filters
            ]
            where = sql.SQL(" WHERE {}").format(sql.SQL(" AND ").join(conditions))
            where_params = list(filters.values())

        try:
            with self._get_cursor(cursor_factory=RealDictCursor) as cur:
                # Total match count (before limit/offset) — a separate query so
                # an out-of-range offset still reports the true total.
                count_stmt = sql.SQL("SELECT COUNT(*) AS total FROM {}{}").format(
                    sql.Identifier(table), where
                )
                self._execute(cur, count_stmt, where_params or None)
                total = int(cur.fetchone()["total"])

                statement = sql.SQL("SELECT * FROM {}{}").format(sql.Identifier(table), where)
                if order_by is not None:
                    direction = sql.SQL("DESC") if descending else sql.SQL("ASC")
                    statement = sql.SQL("{} ORDER BY {} {}").format(
                        statement, sql.Identifier(order_by), direction
                    )
                page_params = list(where_params)
                if limit is not None:
                    statement = sql.SQL("{} LIMIT %s").format(statement)
                    page_params.append(int(limit))
                if offset:
                    statement = sql.SQL("{} OFFSET %s").format(statement)
                    page_params.append(int(offset))

                self._execute(cur, statement, page_params or None)
                rows = [dict(row) for row in cur.fetchall()]
            return rows, total
        except Exception as e:
            logger.error(f"Error in query_page: {e}")
            raise

    def copy_table(self, source: str, dest: str) -> None:
        """Copy a table with transaction support"""
        # Validate table names to prevent SQL injection
        self._validate_identifier(source, "source table")
        self._validate_identifier(dest, "destination table")
        
        conn = None
        try:
            # Get connection without autocommit for transaction
            conn = self._pool.getconn()
            if not conn:
                raise OperationalError("Failed to get connection from pool")
            
            conn.autocommit = False  # Use transaction
            try:
                with conn.cursor() as cur:
                    self._execute(
                        cur,
                        sql.SQL("CREATE TABLE IF NOT EXISTS {dest} (LIKE {src} INCLUDING ALL)").format(
                            dest=sql.Identifier(dest),
                            src=sql.Identifier(source),
                        ),
                    )
                    self._execute(
                        cur,
                        sql.SQL("INSERT INTO {dest} SELECT * FROM {src}").format(
                            dest=sql.Identifier(dest),
                            src=sql.Identifier(source),
                        ),
                    )
                conn.commit()
            except Exception as e:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True  # Restore autocommit
                self._pool.putconn(conn)
        except Exception as e:
            logger.error(f"Error in copy_table: {e}")
            raise

    def insert_columns(
        self,
        table: str,
        columns: List[str],
        values: List[Any] = None,
        conflict_strategy: str = "IGNORE",
    ) -> None:
        # #304.10 — this was a no-op stub whose signature also didn't match
        # DatabaseAdapter.insert_columns (missing values/conflict_strategy), so a
        # call either raised TypeError or, matched positionally, SILENTLY dropped
        # the row on the primary production backend — a latent data-loss trap. Fail
        # loudly instead; there is no caller today, so a real INSERT can be wired
        # in (with sql.Identifier + placeholders honouring conflict_strategy) when
        # one appears. Use insert_item/upsert_item for row writes.
        raise NotImplementedError(
            "PostgresDatabase.insert_columns is not implemented — use insert_item/upsert_item"
        )

    def _get_existing_columns(self, table_name: str) -> Dict[str, Dict[str, str]]:
        """Return current PostgreSQL column metadata keyed by column name."""
        with self._get_cursor(cursor_factory=RealDictCursor) as cur:
            self._execute(
                cur,
                """
                SELECT column_name, data_type, udt_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                """,
                (table_name,)
            )
            return {
                row["column_name"]: {
                    "data_type": row["data_type"],
                    "udt_name": row["udt_name"],
                }
                for row in cur.fetchall()
            }

    def _should_widen_to_bigint(self, expected_type: str, existing_column: Dict[str, str]) -> bool:
        """Return True when an existing integer column should be widened to BIGINT."""
        if expected_type != "BIGINT":
            return False

        current_data_type = (existing_column.get("data_type") or "").lower()
        current_udt_name = (existing_column.get("udt_name") or "").lower()
        return current_data_type == "integer" or current_udt_name == "int4"

    def ensure_table(
        self,
        model: type[BaseModel],
        table_name: str = None
    ) -> None:
        """
        Ensure that a table exists for the given Pydantic model.
        Creates the table with columns based on the Pydantic model fields.
        """
        if table_name is None:
            # Convert CamelCase to snake_case
            table_name = re.sub(r'(?<!^)(?=[A-Z])', '_', model.__name__).lower()
        
        # Validate table name
        self._validate_identifier(table_name, "table name")
        
        try:
            # Get field definitions from Pydantic model
            fields = getattr(model, 'model_fields', None)
            if fields is None:
                fields = getattr(model, '__fields__', {})
            
            # Create column definitions. Types come from _get_postgres_type which
            # returns a fixed, code-controlled allowlist (TEXT, BIGINT, etc.) — wrapped
            # in sql.SQL to compose safely alongside Identifier-quoted names.
            column_defs = [
                sql.SQL("{name} {type}").format(
                    name=sql.Identifier("id"),
                    type=sql.SQL("VARCHAR(255) PRIMARY KEY"),
                )
            ]
            expected_types = {"id": "VARCHAR(255)"}

            for field_name, field_info in fields.items():
                if field_name == 'id':
                    continue

                self._validate_identifier(field_name, f"field name '{field_name}'")

                field_type = field_info.annotation if hasattr(field_info, 'annotation') else field_info
                pg_type = self._get_postgres_type(field_type)
                expected_types[field_name] = pg_type

                is_optional = False
                if hasattr(field_info, 'is_required'):
                    is_optional = not field_info.is_required()
                elif hasattr(field_info, 'default'):
                    is_optional = True
                if hasattr(field_info, 'annotation'):
                    annotation = field_info.annotation
                    if hasattr(annotation, '__origin__') and annotation.__origin__ is Union:
                        if type(None) in annotation.__args__:
                            is_optional = True

                type_clause = pg_type if is_optional else f"{pg_type} NOT NULL"
                column_defs.append(
                    sql.SQL("{name} {type}").format(
                        name=sql.Identifier(field_name),
                        type=sql.SQL(type_clause),
                    )
                )

            create_stmt = sql.SQL("CREATE TABLE IF NOT EXISTS {table} ({cols})").format(
                table=sql.Identifier(table_name),
                cols=sql.SQL(", ").join(column_defs),
            )

            with self._get_cursor() as cur:
                self._execute(cur, create_stmt)
                logger.info(f"Ensured table '{table_name}' exists for model {model.__name__}")

                existing_columns = self._get_existing_columns(table_name)

                for field_name, expected_type in expected_types.items():
                    existing_column = existing_columns.get(field_name)

                    if existing_column is None:
                        null_constraint = " NOT NULL" if field_name == "id" else ""
                        self._execute(
                            cur,
                            sql.SQL("ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {type}").format(
                                table=sql.Identifier(table_name),
                                col=sql.Identifier(field_name),
                                type=sql.SQL(f"{expected_type}{null_constraint}"),
                            ),
                        )
                        logger.info(
                            f"Added missing column '{field_name}' to table '{table_name}' as {expected_type}"
                        )
                        continue

                    if self._should_widen_to_bigint(expected_type, existing_column):
                        self._execute(
                            cur,
                            sql.SQL(
                                "ALTER TABLE {table} ALTER COLUMN {col} TYPE BIGINT USING {col}::BIGINT"
                            ).format(
                                table=sql.Identifier(table_name),
                                col=sql.Identifier(field_name),
                            ),
                        )
                        logger.info(
                            f"Widened column '{field_name}' on table '{table_name}' from INTEGER to BIGINT"
                        )
                
        except Exception as e:
            logger.error(f"Error ensuring table for model {model.__name__}: {e}")
            raise
    
    def _get_postgres_type(self, python_type) -> str:
        """Convert Python/Pydantic type to PostgreSQL type."""
        type_mapping = {
            str: "TEXT",
            int: "BIGINT",
            float: "DOUBLE PRECISION",
            bool: "BOOLEAN",
            dict: "JSONB",
            list: "JSONB",
            datetime.datetime: "TIMESTAMPTZ",
            datetime.date: "DATE",
            datetime.time: "TIMETZ",
        }
        
        # Handle typing annotations
        if hasattr(python_type, '__origin__'):
            origin = python_type.__origin__
            if origin is list:
                return "JSONB"
            elif origin is dict:
                return "JSONB"
            elif origin is Union:
                # For Optional types, get the non-None type
                args = [arg for arg in python_type.__args__ if arg is not type(None)]
                if args:
                    return self._get_postgres_type(args[0])
        
        # Direct type lookup
        if python_type in type_mapping:
            return type_mapping[python_type]
        
        # Default to TEXT for unknown types
        return "TEXT"

    def query(self, sql: str, params: tuple = None) -> List[dict]:
        """Execute raw SQL query with optional parameters"""
        if not sql or not sql.strip():
            raise ValueError("SQL query cannot be empty")
        
        try:
            with self._get_cursor(cursor_factory=RealDictCursor) as cur:
                self._execute(cur, sql, params if params else None)
                if cur.description:
                    return [dict(row) for row in cur.fetchall()]
                # Non-SELECT statements (DDL/DML) have no result set.
                # Return rowcount so callers can report execution outcomes.
                return [{"rowcount": cur.rowcount}]
        except Exception as e:
            logger.error(f"Error in query execution: {e}")
            logger.error(f"SQL: {sql[:200]}...")  # Log first 200 chars
            raise

    def query_readonly(
        self, sql: str, params: tuple = None,
        timeout_ms: int = 10000, row_limit: int = 500,
    ) -> Tuple[List[dict], bool]:
        """Execute sql with writes rejected at the database level and a
        statement timeout — for ad-hoc admin/debug query tools, never for
        the app's own normal read/write paths (use query()/get_item()/etc.
        for those).

        Deliberately does NOT use _get_cursor(): connections from this pool
        have autocommit=True (see _acquire_validated_connection), so under
        autocommit each cursor.execute() is its own implicit transaction —
        a transaction-scoped `SET TRANSACTION READ ONLY`/`SET LOCAL
        statement_timeout` issued as a separate statement would have no
        effect on the *next* execute(). conn.set_session(readonly=True) and
        a session-level (non-LOCAL) statement_timeout persist for the life
        of the connection instead — which is exactly why this connection
        must NEVER be returned to the shared pool afterward (every other
        caller in the app reuses pooled connections for normal writes).
        _discard_connection() closes it outright; the pool opens a fresh one
        next time it's needed.
        """
        if not sql or not sql.strip():
            raise ValueError("SQL query cannot be empty")

        conn = self._acquire_validated_connection()
        try:
            conn.set_session(readonly=True)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(f"SET statement_timeout = {int(timeout_ms)}")
                self._execute(cur, sql, params if params else None)
                rows = cur.fetchmany(row_limit + 1) if cur.description else []
                truncated = len(rows) > row_limit
                return [dict(row) for row in rows[:row_limit]], truncated
        except Exception as e:
            logger.error(f"Error in query_readonly execution: {e}")
            logger.error(f"SQL: {sql[:200]}...")  # Log first 200 chars
            raise
        finally:
            # Never _return_connection() here — see docstring above.
            self._discard_connection(conn)

    def get_binary_item(self, table: str, key: str) -> bytes:
        """Get binary item with validation"""
        self._validate_identifier(table, "table name")

        try:
            with self._get_cursor() as cur:
                stmt = sql.SQL("SELECT data FROM {table} WHERE id = %s").format(
                    table=sql.Identifier(table),
                )
                self._execute(cur, stmt, (key,))
                row = cur.fetchone()
                if row:
                    return row[0]
                raise ValueError(f'Item with key {key} not found in {table}')
        except Exception as e:
            logger.error(f"Error in get_binary_item: {e}")
            raise

    # ─── retention (database.retention / __retention__) ──────────────────
    _TEXT_TYPES = {"text", "character varying", "character"}
    _NUMBER_TYPES = {"smallint", "integer", "bigint", "numeric", "real", "double precision"}

    def _older_than_condition(self, cur, table: str, field: str, data_type: str,
                              cutoff: datetime.datetime) -> Tuple[sql.Composable, list]:
        """SQL condition (+ params) for "field is provably before cutoff",
        chosen by the column's real type."""
        f = sql.Identifier(field)
        cutoff = cutoff.astimezone(datetime.timezone.utc)
        if data_type in self._TEXT_TYPES:
            # ISO-8601 text: the plain text comparison uses the column's
            # b-tree index to find candidates (same leading layout sorts
            # chronologically); the guarded cast then proves each one — a
            # value that isn't a timestamp, or whose offset makes it newer,
            # is kept, never deleted early.
            cond = sql.SQL(
                "{f} < %s AND CASE WHEN {f} ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}[T ][0-9]{{2}}:[0-9]{{2}}' "
                "THEN {f}::timestamptz < %s ELSE false END").format(f=f)
            return cond, [cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), cutoff]
        if data_type == "timestamp with time zone":
            return sql.SQL("{f} < %s").format(f=f), [cutoff]
        if data_type in ("timestamp without time zone", "date"):
            return sql.SQL("{f} < %s").format(f=f), [cutoff.replace(tzinfo=None)]
        if data_type in self._NUMBER_TYPES:
            from database.retention import EPOCH_MS_THRESHOLD
            cur.execute(sql.SQL("SELECT max({f}) FROM {t}").format(f=f, t=sql.Identifier(table)))
            biggest = (cur.fetchone() or [None])[0]
            factor = 1000 if biggest is not None and float(biggest) > EPOCH_MS_THRESHOLD else 1
            return sql.SQL("{f} < %s").format(f=f), [int(cutoff.timestamp() * factor)]
        raise ValueError(f"retention: {table}.{field} is {data_type!r} — not a time column")

    def prune_older_than(self, table: str, field: str, cutoff, *, batch_size: int = 5000,
                         max_rows: Optional[int] = None, dry_run: bool = False) -> int:
        """Batched, index-driven delete of rows whose ``field`` is before
        ``cutoff`` (see DatabaseAdapter.prune_older_than). One pooled
        connection for the whole run: an advisory lock per table keeps
        replicas from pruning the same table at once, and every batch commits
        on its own — short row locks, no huge transaction or WAL spike."""
        self._validate_identifier(table, "table name")
        self._validate_identifier(field, "column name")
        t = sql.Identifier(table)
        with self._get_cursor() as cur:
            cur.execute("SELECT data_type FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
                        (table, field))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"retention: {table}.{field} doesn't exist")
            data_type = row[0] if not isinstance(row, dict) else row["data_type"]
            cond, params = self._older_than_condition(cur, table, field, data_type, cutoff)
            if dry_run:
                cur.execute(sql.SQL("SELECT count(*) FROM {t} WHERE ").format(t=t) + cond, params)
                n = (cur.fetchone() or [0])
                return int(n[0] if not isinstance(n, dict) else list(n.values())[0])
            lock_key = f"cloudseeder.retention.{table}"
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (lock_key,))
            got = cur.fetchone()
            if not (got[0] if not isinstance(got, dict) else list(got.values())[0]):
                logger.info("retention %s: another process is pruning it — skipped", table)
                return 0
            deleted = 0
            try:
                delete = (sql.SQL("DELETE FROM {t} WHERE ctid = ANY(ARRAY(SELECT ctid FROM {t} WHERE ").format(t=t)
                          + cond + sql.SQL(" LIMIT %s))"))
                while max_rows is None or deleted < max_rows:
                    want = batch_size if max_rows is None else min(batch_size, max_rows - deleted)
                    cur.execute(delete, params + [want])
                    n = cur.rowcount or 0
                    if not cur.connection.autocommit:
                        cur.connection.commit()
                    deleted += n
                    if n < want:
                        break
                    time.sleep(0.02)   # let other writers in between batches
            finally:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock_key,))
                if not cur.connection.autocommit:
                    cur.connection.commit()
            return deleted

    def _validate_identifier(self, identifier: str, name: str = "identifier") -> None:
        """Validate SQL identifiers to prevent injection attacks"""
        if not identifier:
            raise ValueError(f"{name} cannot be empty")
        if len(identifier) > 63:
            raise ValueError(f"{name} exceeds PostgreSQL identifier length limit (63 characters)")
        # Allow alphanumeric, underscore, and hyphen
        if not re.match(r'^[a-zA-Z0-9_-]+$', identifier):
            raise ValueError(f"{name} contains invalid characters: {identifier}")

    def list_tables(self) -> List[str]:
        """Return all table names in the current schema."""
        with self._get_cursor() as cur:
            self._execute(
                cur,
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() ORDER BY tablename",
            )
            return [row[0] for row in cur.fetchall()]

    def __del__(self):
        """Cleanup on garbage collection"""
        try:
            self.close()
        except Exception as e:
            # Log but don't raise in __del__
            logger.error(f"Error during cleanup: {e}")
