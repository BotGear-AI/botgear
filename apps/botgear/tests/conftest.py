# unmanaged
"""Shared fixtures for the botgear test suite."""
from typing import Any, Dict, Optional

import pytest


class FakeDB:
    """Minimal in-memory DatabaseAdapter substitute.

    Same shape as the local FakeDB in test_lifecycle.py (that one stays
    put — it predates this and nothing requires unifying them) plus
    ``query_items``, needed by the generated CRUD services'
    ``process_*_request`` handlers.
    """

    def __init__(self) -> None:
        self.tables: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def _table(self, name: str) -> Dict[str, Dict[str, Any]]:
        return self.tables.setdefault(name, {})

    def insert_item(self, table: str, key: str, item: Dict[str, Any]) -> None:
        self._table(table)[key] = dict(item)

    def update_item(self, table: str, key: str, updates: Dict[str, Any], include_nulls: bool = False) -> Dict[str, Any]:
        # Merges into the existing row — matches filesystem_database.py /
        # dynamodb_database.py's real update_item, which is a PATCH, not a
        # replace: None values are dropped unless include_nulls, and the
        # primary key is never overwritten by the updates dict. Getting
        # this wrong once already produced a false-negative on
        # api_client_service's exclude_unset partial-update behavior.
        existing = self._table(table).get(key)
        if existing is None:
            return {}
        filtered = {k: v for k, v in updates.items() if k != "id"}
        if not include_nulls:
            filtered = {k: v for k, v in filtered.items() if v is not None}
        existing.update(filtered)
        return dict(existing)

    # Reads return copies, like every real backend: a caller mutating a
    # fetched row must not silently mutate the stored one (the reset-
    # password route mutates its row, then re-queries by the old token).
    def get_item(self, table: str, key: str) -> Optional[Dict[str, Any]]:
        row = self._table(table).get(key)
        return dict(row) if row is not None else None

    def delete_item(self, table: str, key: str) -> None:
        self._table(table).pop(key, None)

    def get_all_items(self, table: str) -> list:
        return list(self._table(table).values())

    def query_items(self, table_name: str, criteria: Dict[str, Any]) -> list:
        rows = self._table(table_name).values()
        if not criteria:
            return [dict(r) for r in rows]
        return [dict(r) for r in rows if all(r.get(k) == v for k, v in criteria.items())]


@pytest.fixture
def fake_db() -> FakeDB:
    """A fresh in-memory DatabaseAdapter. Callers monkeypatch whichever
    module's ``get_database_client`` should return it."""
    return FakeDB()


@pytest.fixture(autouse=True)
def _unpin_booted_config_set():
    """Reset the config-set boot pin between tests.

    The app-startup path (event_handlers) calls pin_booted_set(), which
    stores the active set name in a module global for the life of the
    process. Any test that drives startup (e.g. the brain files API
    tests) would otherwise leak its pin into every later test —
    active_set_name() then ignores the environment and the env-driven
    expectations in test_config_sets break.
    """
    yield
    from botgear.runtime import config_sets
    config_sets._BOOTED_SET = None
