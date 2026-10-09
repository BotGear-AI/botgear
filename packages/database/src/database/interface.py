from datetime import datetime
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Tuple, Union

# For factory pattern (like MessageConfig)
from pydantic import BaseModel

class DatabaseConfig(BaseModel):
    """Base pydantic config for database clients."""
    name: str

class DatabaseClient(ABC):
    """Abstract interface for database clients (sync, simple)."""
    @abstractmethod
    def insert_item(self, table: str, key: str, item: dict) -> dict:
        pass
    @abstractmethod
    def get_item(self, table: str, key: str) -> dict:
        pass
    @abstractmethod
    def get_all_items(self, table: str) -> list:
        pass
    @abstractmethod
    def delete_item(self, table: str, key: str) -> None:
        pass

class DatabaseAdapter(ABC):

    @abstractmethod
    def upsert_item(self, table: str, key: str, item: dict) -> dict:
        """Upsert an item into the specified table."""
        pass

    @abstractmethod
    def insert_item(self, table: str, key:str, item: dict) -> dict:
        """Insert an item into the specified table."""
        pass

    @abstractmethod
    def get_item(self, table: str, key: str) -> dict:
        """Retrieve an item by its key from the specified table."""
        pass

    @abstractmethod
    def get_binary_item(self, table: str, key: str) -> bytes:
        """Retrieve an item by its key from the specified table."""
        pass

    @abstractmethod
    def get_all_items(self, table: str) -> list:
        """Retrieve all items from the specified table."""
        pass

    @abstractmethod
    def update_item(self, table: str, key: str, updates: dict, include_nulls: bool = False) -> dict:
        """
        Update an item in the specified table.

        :param include_nulls: When False (default), None values in updates are ignored.
        """
        pass

    def bulk_update_items(
        self, table: str, updates_by_id: Dict[str, dict], include_nulls: bool = False
    ) -> List[dict]:
        """Apply a per-row update to many rows of ``table`` at once.

        ``updates_by_id`` maps row id -> the same-shaped ``updates`` dict
        ``update_item`` takes. Returns the updated rows (order not
        guaranteed to match input order).

        Non-abstract on purpose, same rationale as ``query_page``: this
        default just calls ``update_item`` once per row, so every adapter
        (including document/key-value backends with no native multi-row
        UPDATE) gets correct behavior for free. It exists so a caller
        writing hundreds/thousands of rows in one logical operation (e.g.
        re-evaluating a batch of records against a rule set) has a single
        entry point to call — adapters that can push a real multi-row
        UPDATE down into the engine (Postgres, SQL backends) override this
        to collapse what would otherwise be one network round-trip per row
        into a small, constant number of round-trips for the whole batch.
        Confirmed in practice: 976 sequential update_item() calls against
        Postgres took ~53s (each paying its own connection-pool checkout +
        validation ping + single-row UPDATE); the point of overriding this
        is to turn that into a handful of multi-row statements instead.
        """
        results = []
        for row_id, updates in updates_by_id.items():
            result = self.update_item(table, row_id, updates, include_nulls=include_nulls)
            if result:
                results.append(result)
        return results

    def compare_and_update_item(self, table: str, key: str, expected: dict, updates: dict) -> bool:
        """Atomically update scalar fields only if the stored row matches expected.

        Return False on a missing row or mismatch. Unsupported adapters must
        raise rather than approximate this operation with separate reads/writes.
        """
        raise NotImplementedError("Atomic conditional updates are not supported")

    @abstractmethod
    def delete_item(self, table: str, key: str) -> None:
        """Delete an item from the specified table by its key."""
        pass

    @abstractmethod
    def query_items(self, table_name: str, criteria: dict) -> list:
        """Query the database for items matching the given criteria."""
        pass

    def query_page(
        self,
        table: str,
        *,
        filters: Optional[Dict[str, Any]] = None,
        order_by: Optional[str] = None,
        descending: bool = False,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Return one page of rows plus the total count of rows matching ``filters``.

        This is the pagination primitive behind the generated ``__paginate__``
        list route. The contract is uniform across every backend:

        - ``filters``   — equality criteria (same shape as ``query_items``).
        - ``order_by``  — column to sort by (None = backend/natural order).
        - ``descending``— sort direction.
        - ``limit``/``offset`` — the slice to return (page_size / page window).
        - returns ``(rows, total)`` where ``total`` is the full match count
          *before* limit/offset — so callers can compute page counts.

        Non-abstract on purpose: this default materializes the filtered set and
        sorts/slices in Python, so it works on **every** adapter (including the
        document/key-value backends that have no native paging — tinydb, s3,
        filesystem, dynamodb). Adapters that can push ORDER BY / LIMIT / OFFSET /
        COUNT down into the engine (Postgres, SQLite, Mongo) override this for
        scalability. Cost of the default is O(table) per page; fine for small
        tables, override for large ones.
        """
        rows = self.query_items(table, filters or {})
        total = len(rows)
        if order_by:
            def _key(row: Dict[str, Any]):
                value = row.get(order_by)
                # (is_none, value) keeps NULLs last on ascending sorts; the
                # str() fallback avoids a TypeError when a column holds mixed
                # types across rows in a schemaless backend.
                return (value is None, value)
            try:
                rows = sorted(rows, key=_key, reverse=descending)
            except TypeError:
                rows = sorted(
                    rows,
                    key=lambda r: (r.get(order_by) is None, str(r.get(order_by))),
                    reverse=descending,
                )
        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return rows, total

    def prune_older_than(
        self,
        table: str,
        field: str,
        cutoff: datetime,
        *,
        batch_size: int = 5000,
        max_rows: Optional[int] = None,
        dry_run: bool = False,
    ) -> int:
        """Delete rows whose ``field`` is a point in time before ``cutoff``
        (an aware UTC datetime); returns how many were deleted (or would be,
        with ``dry_run``). The primitive behind ``__retention__`` — see
        ``database.retention`` for policies, scheduling and the safety rails.

        A row is deleted only when its value is provably older: NULL or
        unparseable values are kept. Values may be ISO-8601 strings,
        datetimes or epoch numbers (ms above 1e11, else seconds).

        Non-abstract on purpose, like ``query_page``: this default scans the
        table in Python and deletes row by row, so it works on **every**
        adapter; Postgres and TinyDB override it. O(table) — fine for small
        tables only.
        """
        from database.retention import is_older
        doomed = [row.get("id") for row in self.get_all_items(table)
                  if row.get("id") is not None and is_older(row.get(field), cutoff)]
        if max_rows is not None:
            doomed = doomed[:max_rows]
        if not dry_run:
            for key in doomed:
                self.delete_item(table, key)
        return len(doomed)

    @abstractmethod
    def search_by_key_part(
        self, table: str, key_part: str, regex: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Search for items whose keys contain or match a part of the given key.

        :param table: The table to search in.
        :param key_part: The key part to search for.
        :param regex: Whether to treat key_part as a regular expression. Defaults to False (prefix search).
        :return: A list of matching items.
        """
        pass

    @abstractmethod
    def copy_table(self, source_table: str, dest_table: str) -> None:
        """
        Copy all items from source_table to dest_table.
        """
        pass

    @abstractmethod
    def query(self, querystr: str, params: tuple = None) -> list:
        """
        Execute a backend-specific query string and return a list of results.
        For SQL backends, this is a SQL query with optional parameters. For others, NotImplementedError is raised.
        """
        pass

    def query_readonly(
        self, querystr: str, params: tuple = None,
        timeout_ms: int = 10000, row_limit: int = 500,
    ) -> Tuple[List[dict], bool]:
        """
        Execute querystr with writes rejected at the database level and a
        statement timeout, for ad-hoc admin/debug query tools. Returns
        (rows, truncated) — truncated is True when more than row_limit rows
        were available.

        Non-abstract on purpose, matching list_tables(): existing adapters
        continue to work unchanged; only backends that opt in (e.g. for an
        admin SQL-debug page) need to implement it. Raising NotImplementedError
        by default means a backend that hasn't implemented a real read-only
        guard can never silently allow writes through this path.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement query_readonly(). "
            "Ad-hoc read-only query tools are unavailable for this backend."
        )


    @abstractmethod
    def insert_columns(
        self,
        table: str,
        columns: List[str],
        values: List[Any],
        conflict_strategy: str = "IGNORE"  # or "REPLACE", "FAIL", "ABORT"
    ) -> None:
        """
        Insert a new row into the table with specified columns and values.

        :param table: Table name.
        :param columns: List of column names to insert into.
        :param values: List of values corresponding to the columns.
        :param conflict_strategy: Optional SQL conflict strategy (e.g., IGNORE, REPLACE).
        """
        pass

    @abstractmethod
    def ensure_table(
        self,
        model: type[BaseModel],
        table_name: str = None
    ) -> None:
        """
        Ensure that a table exists for the given Pydantic model.

        :param model: The Pydantic model class to create a table for.
        :param table_name: Optional table name. If None, will use the lowercase snake_case name of the model.
        """
        pass

    def list_tables(self) -> List[str]:
        """
        Return the names of every table currently materialised in this backend.
        Non-abstract on purpose: existing adapters continue to work; only the
        ones that opt in (e.g. for admin / introspection UIs) need to implement it.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement list_tables(). "
            "Admin/introspection features that depend on enumerating tables "
            "are unavailable for this backend."
        )
