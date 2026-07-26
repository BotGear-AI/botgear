# unmanaged
"""Tests for runtime.workspaces — specifically reconcile_running_proxies'
registry-ordered restart on boot.

reconcile revives services whose rows still say running/starting from the
previous backend lifetime (their processes are gone). The boot registry is
passed in so the restarts fire in DECLARED order — registry members first,
in registry order — instead of dict-iteration order.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SRC = Path(__file__).parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from botcore.runtime import process_manager  # noqa: E402
from botcore.runtime import lifecycle  # noqa: E402
from botcore.runtime.workspaces import reconcile_running_proxies  # noqa: E402


class StubDB:
    def __init__(self):
        self.tables: Dict[str, Dict[str, Any]] = {"service_proxy": {}, "service_meta": {}}

    def get_all_items(self, table: str) -> List[Dict[str, Any]]:
        return list(self.tables.get(table, {}).values())

    def get_item(self, table: str, key: str) -> Dict[str, Any]:
        # Mirror the real adapter: missing → {} (not None).
        result = self.tables.get(table, {}).get(key)
        return result if result else {}

    def insert_item(self, table: str, key: str, row: Dict[str, Any]) -> None:
        self.tables.setdefault(table, {})[key] = dict(row)

    def update_item(self, table: str, key: str, updates: Dict[str, Any], *, include_nulls=True) -> Dict[str, Any]:
        # Mirror the real adapter: skip when absent (no upsert), else merge.
        existing = self.tables.setdefault(table, {}).get(key)
        if not existing:
            return {}
        existing.update({k: v for k, v in updates.items() if k != "id"})
        return existing

    def delete_item(self, table: str, key: str) -> None:
        self.tables.setdefault(table, {}).pop(key, None)


def _seed_running(db: StubDB, *proxy_ids: str) -> None:
    for pid in proxy_ids:
        db.insert_item("service_proxy", pid, {
            "id": pid,
            "service_meta_id": "clock@1.0.0",
            "status": "running",
            "pid": 999999,          # a pid we'll report dead
        })


def test_reconcile_orders_restarts_by_registry(monkeypatch):
    """Given rows inserted in one order and a registry in another, the
    restarts fire in REGISTRY order (members first, in listed order)."""
    db = StubDB()
    _seed_running(db, "z-1", "a-1", "m-1")     # insertion order z, a, m

    started: List[str] = []
    monkeypatch.setattr(process_manager, "pid_alive", lambda pid: False)
    monkeypatch.setattr(
        lifecycle, "_handle_start",
        lambda msg, _db: started.append(msg.get("service_proxy_id")),
    )

    reconcile_running_proxies(db, ["m-1", "a-1", "z-1"])
    assert started == ["m-1", "a-1", "z-1"]


def test_reconcile_registry_members_first_then_unlisted(monkeypatch):
    """Rows not named in the registry are a defensive tail — restarted after
    every registry member, preserving their relative order."""
    db = StubDB()
    _seed_running(db, "extra-1", "a-1", "b-1", "extra-2")

    started: List[str] = []
    monkeypatch.setattr(process_manager, "pid_alive", lambda pid: False)
    monkeypatch.setattr(
        lifecycle, "_handle_start",
        lambda msg, _db: started.append(msg.get("service_proxy_id")),
    )

    reconcile_running_proxies(db, ["a-1", "b-1"])   # extras not listed
    assert started[:2] == ["a-1", "b-1"]            # registry first, in order
    assert set(started[2:]) == {"extra-1", "extra-2"}   # tail = the unlisted rows


def test_reconcile_skips_live_and_non_running(monkeypatch):
    """A row whose pid is still alive is left alone (no duplicate spawn); a
    stopped row is not a restart candidate."""
    db = StubDB()
    _seed_running(db, "alive-1", "dead-1")
    db.insert_item("service_proxy", "stopped-1", {
        "id": "stopped-1", "service_meta_id": "clock@1.0.0", "status": "stopped",
    })

    started: List[str] = []
    monkeypatch.setattr(process_manager, "pid_alive", lambda pid: pid == 111)  # only alive-1
    db.tables["service_proxy"]["alive-1"]["pid"] = 111
    db.tables["service_proxy"]["dead-1"]["pid"] = 222
    monkeypatch.setattr(
        lifecycle, "_handle_start",
        lambda msg, _db: started.append(msg.get("service_proxy_id")),
    )

    reconcile_running_proxies(db, ["alive-1", "dead-1", "stopped-1"])
    assert started == ["dead-1"]     # alive skipped, stopped not a candidate


def test_reconcile_skips_singletons(monkeypatch):
    """Singleton services (tagged in service_meta) ride the restart and must
    not be re-spawned here."""
    db = StubDB()
    db.insert_item("service_meta", "runtime@1.0.0", {"tags": ["singleton"]})
    db.insert_item("service_proxy", "runtime", {
        "id": "runtime", "service_meta_id": "runtime@1.0.0",
        "status": "running", "pid": 999999,
    })
    _seed_running(db, "clock-1")

    started: List[str] = []
    monkeypatch.setattr(process_manager, "pid_alive", lambda pid: False)
    monkeypatch.setattr(
        lifecycle, "_handle_start",
        lambda msg, _db: started.append(msg.get("service_proxy_id")),
    )

    reconcile_running_proxies(db, ["runtime", "clock-1"])
    assert started == ["clock-1"]    # singleton not restarted
