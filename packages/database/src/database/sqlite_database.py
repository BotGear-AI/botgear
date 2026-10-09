import sqlite3
import os
import logging
import re
import json
from typing import Dict, List, Any, Optional, Tuple, Union
from .interface import DatabaseAdapter
from .record_transformer import RecordTransformer
import threading
from pydantic import BaseModel

try:
    from pydantic.fields import Undefined
except ImportError:  # pragma: no cover - depends on Pydantic version
    try:
        from pydantic_core import PydanticUndefined as Undefined
    except ImportError:  # pragma: no cover
        Undefined = object()

logger = logging.getLogger(__name__)

from models.database_sqlite_config import DatabaseSqliteConfig

class SqliteDatabase(DatabaseAdapter):
    # One instance PER sqlite_path (#298). The old single _instance meant a second
    # SqliteDatabase(config_with_different_path) silently returned the first
    # instance bound to the first file, routing its reads/writes to the wrong db.
    # Same-path callers still share an instance; distinct paths are isolated.
    _instances: dict = {}
    _lock = threading.Lock()

    def __new__(cls, config: DatabaseSqliteConfig):
        path = config.sqlite_path
        inst = cls._instances.get(path)
        if inst is None:
            with cls._lock:
                inst = cls._instances.get(path)
                if inst is None:
                    inst = super(SqliteDatabase, cls).__new__(cls)
                    inst._initialized = False
                    cls._instances[path] = inst
        return inst

    def __init__(self, config: DatabaseSqliteConfig):
        if getattr(self, '_initialized', False):
            return
        # Serialize init and set _initialized ONLY after it succeeds: two threads
        # sharing a just-created instance must not both run init, and a failure in
        # makedirs/connect must not leave a half-built instance cached with
        # _initialized=True (later callers would then get an object missing .conn/
        # .transformer). On failure _initialized stays False so the next call retries.
        with self._lock:
            if getattr(self, '_initialized', False):
                return
            self.config = config
            dirpath = os.path.dirname(self.config.sqlite_path)
            if dirpath:
                os.makedirs(dirpath, exist_ok=True)
            self.conn = sqlite3.connect(self.config.sqlite_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            # WAL is a database-level setting persisted in the file header, so
            # setting it once here covers every connection opened later via
            # _get_connection() — readers no longer block writers, which matters
            # for a live FastAPI + background-bus process hitting this file from
            # multiple threads concurrently.
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.transformer = RecordTransformer()
            self._initialized = True
            logger.info(f"SqliteDatabase initialized at {self.config.sqlite_path}")

    def _get_connection(self):
        """Get a new SQLite connection for each operation (thread-safe)."""
        conn = sqlite3.connect(self.config.sqlite_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    _IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    @classmethod
    def _safe_ident(cls, ident: str, kind: str = "identifier") -> str:
        """Validate then quote a SQL identifier. sqlite3 has no parameterized-identifier
        API like psycopg2.sql.Identifier, so the only safe path is allowlist + quote.
        Accepts identifiers matching ``[A-Za-z_][A-Za-z0-9_]*`` and returns them
        wrapped in double quotes (sqlite's standard delimited-identifier syntax).
        Anything else raises ValueError before reaching the engine.
        """
        if not isinstance(ident, str) or not cls._IDENT_RE.match(ident):
            raise ValueError(f"invalid {kind}: {ident!r}")
        return f'"{ident}"'

    def _ensure_columns(self, conn: sqlite3.Connection, table_ident: str, columns: List[str]) -> None:
        """Add any of ``columns`` missing from the live table, as nullable TEXT.

        A write can carry a key that was never declared on the Pydantic model
        passed to ``ensure_table`` at all — e.g. botgear's config/default seed
        (event_handlers.py) injects a ``created_at`` key ad hoc, outside the
        Config model's fields. TinyDB's schemaless JSON absorbs that silently;
        sqlite needs the column to exist first. Called from insert_item/
        upsert_item so every write is self-healing, not just ones that
        happen to follow a matching ensure_table() call.
        """
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table_ident})")
        existing = {row[1] for row in cur.fetchall()}
        for col in columns:
            if col in existing:
                continue
            cur.execute(f"ALTER TABLE {table_ident} ADD COLUMN {self._safe_ident(col, f'column {col!r}')} TEXT")

    def upsert_item(self, table: str, key: str, item: dict) -> dict:
        t = self._safe_ident(table, "table name")
        flat_item = self.transformer.flatten(item)
        columns = list(flat_item.keys())
        values = [flat_item[col] for col in columns]
        if 'id' not in columns:
            columns = ['id'] + columns
            values = [key] + values
        cols_sql = ', '.join(self._safe_ident(c, f"column '{c}'") for c in columns)
        placeholders = ', '.join(['?'] * len(values))
        conn = self._get_connection()
        cur = conn.cursor()
        self._ensure_columns(conn, t, columns)
        cur.execute(f"INSERT OR REPLACE INTO {t} ({cols_sql}) VALUES ({placeholders})", values)
        conn.commit()
        conn.close()
        return item

    def insert_item(self, table: str, key: str, item: dict) -> dict:
        """Insert a new row, refusing to clobber an existing one.

        Mirrors TinyDBDatabase.insert_item and PostgresDatabase.insert_item:
        both refuse (raise) on a duplicate key rather than silently
        overwriting — callers such as botgear's first-user-claim race guard
        (``first_user_routes.py``) depend on insert_item raising to detect a
        concurrent duplicate. Previously this method aliased to
        ``upsert_item`` (``INSERT OR REPLACE``), which silently overwrote on
        a duplicate key and broke that guard under sqlite.
        """
        t = self._safe_ident(table, "table name")
        flat_item = self.transformer.flatten(item)
        columns = list(flat_item.keys())
        values = [flat_item[col] for col in columns]
        if 'id' not in columns:
            columns = ['id'] + columns
            values = [key] + values
        cols_sql = ', '.join(self._safe_ident(c, f"column '{c}'") for c in columns)
        placeholders = ', '.join(['?'] * len(values))
        conn = self._get_connection()
        cur = conn.cursor()
        self._ensure_columns(conn, t, columns)
        try:
            cur.execute(f"INSERT INTO {t} ({cols_sql}) VALUES ({placeholders})", values)
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            raise ValueError(f"insert_item: '{key}' already exists in table '{table}'")
        conn.close()
        return item

    def get_item(self, table: str, key: str) -> dict:
        t = self._safe_ident(table, "table name")
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({t})")
        columns = [row[1] for row in cur.fetchall()]
        cur.execute(f"SELECT * FROM {t} WHERE id = ?", (key,))
        row = cur.fetchone()
        conn.close()
        if row:
            row_dict = dict(zip(columns, row))
            return self.transformer.unflatten(row_dict)
        return {}

    def get_binary_item(self, table: str, key: str) -> bytes:
        t = self._safe_ident(table, "table name")
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(f"PRAGMA table_info({t})")
            columns = [row[1] for row in cur.fetchall()]
            if not columns:
                raise ValueError(f"Table {table} does not exist")

            if "json" in columns:
                cur.execute(f"SELECT json FROM {t} WHERE id = ?", (key,))
                row = cur.fetchone()
                if row and row[0] is not None:
                    data = row[0]
                    if isinstance(data, (bytes, bytearray)):
                        return bytes(data)
                    return str(data).encode("utf-8")
            else:
                cur.execute(f"SELECT * FROM {t} WHERE id = ?", (key,))
                row = cur.fetchone()
                if row:
                    record = dict(zip(columns, row))
                    payload = json.dumps(
                        record,
                        default=lambda obj: obj.decode("utf-8") if isinstance(obj, (bytes, bytearray)) else str(obj)
                    )
                    return payload.encode("utf-8")
            raise ValueError(f"Item with key {key} not found in {table}")
        finally:
            conn.close()

    def get_all_items(self, table: str) -> list:
        try:
            t = self._safe_ident(table, "table name")
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute(f"PRAGMA table_info({t})")
            columns = [row[1] for row in cur.fetchall()]
            cur.execute(f"SELECT * FROM {t}")
            rows = cur.fetchall()
            conn.close()
            return [self.transformer.unflatten(dict(zip(columns, row))) for row in rows]
        except Exception as e:
            logger.error(f"Error retrieving all items from {table}: {e}")
            return []

    def update_item(self, table: str, key: str, updates: dict, include_nulls: bool = False) -> dict:
        item = self.get_item(table, key)
        if not item:
            return {}
        filtered_updates = {k: v for k, v in updates.items() if k != "id"}
        if not include_nulls:
            filtered_updates = {k: v for k, v in filtered_updates.items() if v is not None}
        if not filtered_updates:
            logger.warning("update_item called with no applicable fields for sqlite table %s", table)
            return item
        item.update(filtered_updates)
        return self.upsert_item(table, key, item)

    def compare_and_update_item(self, table: str, key: str, expected: dict, updates: dict) -> bool:
        table_name = self._safe_ident(table, "table name")
        if not expected or not updates or "id" in updates:
            raise ValueError("Expected fields and updates are required; id cannot be updated")
        assignments = ", ".join(f"{self._safe_ident(column, 'column name')} = ?" for column in updates)
        conditions = " AND ".join(
            f"({self._safe_ident(column, 'column name')} IS NULL OR {self._safe_ident(column, 'column name')} IS ?)"
            if value is None else f"{self._safe_ident(column, 'column name')} IS ?"
            for column, value in expected.items()
        )
        stored_updates = self.transformer.flatten(updates)
        stored_expected = self.transformer.flatten(expected)
        connection = self._get_connection()
        try:
            with connection:
                cursor = connection.execute(
                    f"UPDATE {table_name} SET {assignments} WHERE id = ? AND {conditions}",
                    [*stored_updates.values(), key, *stored_expected.values()],
                )
                return cursor.rowcount == 1
        finally:
            connection.close()

    def delete_item(self, table: str, key: str) -> None:
        t = self._safe_ident(table, "table name")
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {t} WHERE id = ?", (key,))
        conn.commit()
        conn.close()

    def query_items(self, table_name: str, criteria: dict) -> list:
        t = self._safe_ident(table_name, "table name")
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({t})")
        columns = [row[1] for row in cur.fetchall()]
        cur.execute(f"SELECT * FROM {t}")
        rows = cur.fetchall()
        conn.close()
        results = []
        for row in rows:
            row_dict = dict(zip(columns, row))
            item = self.transformer.unflatten(row_dict)
            if all(item.get(k) == v for k, v in criteria.items()):
                results.append(item)
        return results

    # NOTE: no query_page override. SQLite stores flattened values with TEXT
    # affinity, so a pushed-down `ORDER BY <int_col>` sorts lexicographically
    # (…"19","2"…) and diverges from the base default's Python-typed ordering.
    # Rather than ship a subtly-wrong sort, SQLite uses the correct in-memory
    # base implementation (DatabaseAdapter.query_page). Native push-down is
    # reserved for the strongly-typed backends (Postgres, Mongo).

    def search_by_key_part(self, table: str, key_part: str, regex: bool = False) -> List[Dict[str, Any]]:
        t = self._safe_ident(table, "table name")
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({t})")
        columns = [row[1] for row in cur.fetchall()]
        cur.execute(f"SELECT * FROM {t}")
        rows = cur.fetchall()
        conn.close()
        results = []
        for row in rows:
            row_dict = dict(zip(columns, row))
            item = self.transformer.unflatten(row_dict)
            id_val = row_dict.get('id', '')
            if regex:
                if re.search(key_part, id_val):
                    results.append(item)
            else:
                if id_val.startswith(key_part):
                    results.append(item)
        return results

    def copy_table(self, source_table: str, dest_table: str) -> None:
        src = self._safe_ident(source_table, "source table")
        # dest_table is validated inside the subsequent upsert_item path.
        # Uses upsert_item (not insert_item) to match TinyDBDatabase.copy_table,
        # which overwrites via a raw insert() with no duplicate-key check —
        # insert_item's new refuse-on-duplicate behavior would otherwise make
        # this raise on a re-run against a non-empty dest_table.
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({src})")
        columns = [row[1] for row in cur.fetchall()]
        cur.execute(f"SELECT * FROM {src}")
        rows = cur.fetchall()
        conn.close()
        for row in rows:
            row_dict = dict(zip(columns, row))
            item = self.transformer.unflatten(row_dict)
            self.upsert_item(dest_table, row_dict['id'], item)

    def list_tables(self) -> List[str]:
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        names = [row[0] for row in cur.fetchall()]
        conn.close()
        return names

    def query(self, querystr: str) -> list:
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute(querystr)
        if cur.description:  # SELECT or similar
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            conn.close()
            # Always unflatten rows before returning
            return [self.transformer.unflatten(dict(zip(columns, row))) for row in rows]
        else:  # INSERT, UPDATE, DELETE, etc.
            conn.commit()
            conn.close()
            return []

    def insert_columns(
        self,
        table: str,
        columns: List[str],
        values: List[Any],
        conflict_strategy: str = "IGNORE"
    ) -> None:
        """
        Insert a new row into the table with specified columns and values.
        """
        if len(columns) != len(values):
            raise ValueError(f"Number of columns ({len(columns)}) must match number of values ({len(values)})")

        if conflict_strategy not in {"IGNORE", "REPLACE", "ABORT", "FAIL", "ROLLBACK"}:
            raise ValueError(f"invalid conflict_strategy: {conflict_strategy!r}")

        t = self._safe_ident(table, "table name")
        cols_sql = ', '.join(self._safe_ident(c, f"column '{c}'") for c in columns)
        placeholders = ', '.join(['?'] * len(values))
        conn = self._get_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                f"INSERT OR {conflict_strategy} INTO {t} ({cols_sql}) VALUES ({placeholders})",
                values,
            )
            conn.commit()
            conn.close()
            logger.debug(f"Inserted row into {table} with columns {columns}")
        except Exception as e:
            logger.error(f"Failed to insert into {table}: {e}")
            conn.close()
            raise
    
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

        t = self._safe_ident(table_name, "table name")

        try:
            # Get field definitions from Pydantic model
            if hasattr(model, "model_fields"):
                fields = model.model_fields
            else:
                fields = getattr(model, "__fields__", {})

            # SQLite types come from a fixed allowlist (_get_sqlite_type); the only
            # caller-controlled tokens are field/column names, which we run through
            # _safe_ident below.
            field_columns: Dict[str, Tuple[str, str]] = {}  # name -> (sqlite_type, null_constraint)

            for field_name, field_info in fields.items():
                if field_name == 'id':
                    continue

                field_type = field_info.annotation if hasattr(field_info, 'annotation') else field_info
                sqlite_type = self._get_sqlite_type(field_type)

                is_optional = False
                if hasattr(field_info, 'is_required'):
                    is_required_attr = field_info.is_required
                    if callable(is_required_attr):
                        is_optional = not bool(is_required_attr())
                    else:
                        is_optional = not bool(is_required_attr)

                default_value = getattr(field_info, 'default', Undefined)
                if default_value is not Undefined:
                    is_optional = True

                default_factory = getattr(field_info, 'default_factory', None)
                if default_factory not in (None, Undefined):
                    is_optional = True

                if hasattr(field_info, 'annotation'):
                    annotation = field_info.annotation
                    if hasattr(annotation, '__origin__') and annotation.__origin__ is Union:
                        if type(None) in getattr(annotation, '__args__', ()):
                            is_optional = True

                null_constraint = "" if is_optional else " NOT NULL"
                field_columns[field_name] = (sqlite_type, null_constraint)

            columns = [f"{self._safe_ident('id', 'column id')} TEXT PRIMARY KEY"] + [
                f"{self._safe_ident(name, f'column {name!r}')} {sqlite_type}{null_constraint}"
                for name, (sqlite_type, null_constraint) in field_columns.items()
            ]
            columns_sql = ",\n    ".join(columns)
            create_sql = f"CREATE TABLE IF NOT EXISTS {t} (\n    {columns_sql}\n)"

            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute(create_sql)

            # Schema drift: a model that gains a field after its table already
            # exists (routine — see AGENTS.md's "add a model field, regenerate"
            # workflow) must not start crashing every insert against the live
            # table. TinyDB's schemaless JSON absorbs a new field for free;
            # sqlite needs an explicit ALTER TABLE ADD COLUMN per missing one.
            # Added columns are always nullable — existing rows have no value
            # to backfill, and sqlite can't add a NOT NULL column without a
            # default to a non-empty table.
            cur.execute(f"PRAGMA table_info({t})")
            existing_columns = {row[1] for row in cur.fetchall()}
            for name, (sqlite_type, _null_constraint) in field_columns.items():
                if name in existing_columns:
                    continue
                cur.execute(
                    f"ALTER TABLE {t} ADD COLUMN {self._safe_ident(name, f'column {name!r}')} {sqlite_type}"
                )
                logger.info(f"Added column '{name}' to table '{table_name}' for model {model.__name__}")

            conn.commit()
            conn.close()
            logger.info(f"Ensured table '{table_name}' exists for model {model.__name__}")

        except Exception as e:
            logger.error(f"Error ensuring table for model {model.__name__}: {e}")
            raise
    
    def _get_sqlite_type(self, python_type) -> str:
        """Convert Python/Pydantic type to SQLite type."""
        type_mapping = {
            str: "TEXT",
            int: "INTEGER", 
            float: "REAL",
            bool: "INTEGER",  # SQLite stores booleans as integers
            dict: "TEXT",     # Store as JSON text
            list: "TEXT",     # Store as JSON text
        }
        
        # Handle typing annotations
        if hasattr(python_type, '__origin__'):
            origin = python_type.__origin__
            if origin is list:
                return "TEXT"  # Store as JSON
            elif origin is dict:
                return "TEXT"  # Store as JSON
            elif origin is Union:
                # For Optional types, get the non-None type
                args = [arg for arg in python_type.__args__ if arg is not type(None)]
                if args:
                    return self._get_sqlite_type(args[0])
        
        # Direct type lookup
        if python_type in type_mapping:
            return type_mapping[python_type]
        
        # Default to TEXT for unknown types
        return "TEXT"
