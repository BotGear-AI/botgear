# unmanaged
"""Unit tests for the reconciler's core logic: _reconcile_once (the
per-pass sweep) and _scan_service_subprocesses (ownership classification
for orphan cleanup).

Scope: start()/stop()/_consume_loop()/_thread_main() are thin
threading/asyncio orchestration around _reconcile_once — not covered
here; a real test of "does the daemon thread actually loop every 15s"
buys little for a lot of flakiness risk. What matters is the sweep
logic itself, which is synchronous and takes `db` as a plain argument.

_publish_lifecycle/_tidy_workspace_refs and the reconciler's own
get_bus().publish_sync(...) run against the REAL in-process bus (same
convention test_lifecycle.py uses) rather than being mocked — it's
pure in-memory pub/sub with no external side effects, and every proxy
id used here is test-unique so nothing collides across tests.
"""
from __future__ import annotations

import os
from typing import Any, Dict

import pytest

from botgear.runtime import process_manager, proc_compat, reconciler


@pytest.fixture(autouse=True)
def _clean_orphan_tracking():
    """_orphan_sigterm_at is module-global (SIGTERM->SIGKILL escalation
    state deliberately survives across reconcile passes) — reset it
    around each test so tests don't leak into each other."""
    reconciler._orphan_sigterm_at.clear()
    yield
    reconciler._orphan_sigterm_at.clear()


@pytest.fixture
def no_orphans(monkeypatch):
    """Most _reconcile_once tests don't care about the orphan-process
    scan — stub it to empty so section 2 of the sweep is a no-op."""
    monkeypatch.setattr(reconciler, "_scan_service_subprocesses", lambda: [])


# ─── _reconcile_once: stale running/starting rows ───────────────────────

def test_marks_a_dead_pid_as_error(fake_db, monkeypatch, no_orphans):
    monkeypatch.setattr(process_manager, "pid_alive", lambda pid: False)
    fake_db.insert_item("service_proxy", "cam-1", {"id": "cam-1", "status": "running", "pid": 1234})

    stats = reconciler._reconcile_once(fake_db)

    row = fake_db.get_item("service_proxy", "cam-1")
    assert row["status"] == "error"
    assert row["pid"] is None
    assert row["error"] == "reconciler detected dead pid"
    assert row["stopped_at"]
    assert stats["stale_rows_marked_error"] == 1


def test_leaves_a_live_pid_alone(fake_db, monkeypatch, no_orphans):
    monkeypatch.setattr(process_manager, "pid_alive", lambda pid: True)
    fake_db.insert_item("service_proxy", "cam-1", {"id": "cam-1", "status": "running", "pid": 1234})

    stats = reconciler._reconcile_once(fake_db)

    assert fake_db.get_item("service_proxy", "cam-1")["status"] == "running"
    assert stats["stale_rows_marked_error"] == 0


@pytest.mark.parametrize("status", ["stopped", "placeholder", "installed", "error"])
def test_ignores_rows_that_are_not_running_or_starting(fake_db, monkeypatch, no_orphans, status):
    monkeypatch.setattr(process_manager, "pid_alive", lambda pid: False)
    fake_db.insert_item("service_proxy", "cam-1", {"id": "cam-1", "status": status, "pid": None})
    stats = reconciler._reconcile_once(fake_db)
    assert fake_db.get_item("service_proxy", "cam-1")["status"] == status
    assert stats["stale_rows_marked_error"] == 0


def test_refreshes_a_singleton_pid_instead_of_marking_it_error(fake_db, monkeypatch, no_orphans):
    monkeypatch.setattr(process_manager, "pid_alive", lambda pid: False)
    fake_db.insert_item("service_meta", "runtime@1.0.0", {"id": "runtime@1.0.0", "tags": ["singleton"]})
    fake_db.insert_item(
        "service_proxy", "runtime",
        {"id": "runtime", "status": "running", "pid": 999999, "service_meta_id": "runtime@1.0.0"},
    )

    stats = reconciler._reconcile_once(fake_db)

    row = fake_db.get_item("service_proxy", "runtime")
    assert row["status"] == "running"
    assert row["pid"] == os.getpid()
    assert stats["stale_rows_marked_error"] == 0


def test_singleton_already_matching_this_pid_is_untouched(fake_db, monkeypatch, no_orphans):
    monkeypatch.setattr(process_manager, "pid_alive", lambda pid: True)
    fake_db.insert_item("service_meta", "runtime@1.0.0", {"id": "runtime@1.0.0", "tags": ["singleton"]})
    fake_db.insert_item(
        "service_proxy", "runtime",
        {"id": "runtime", "status": "running", "pid": os.getpid(), "service_meta_id": "runtime@1.0.0"},
    )
    reconciler._reconcile_once(fake_db)
    assert fake_db.get_item("service_proxy", "runtime")["pid"] == os.getpid()


# ─── _reconcile_once: orphan subprocess cleanup ─────────────────────────

def _orphan(pid: int, cmdline: str = "python -m stub_service", ppid: int = 1) -> Dict[str, Any]:
    return {"pid": pid, "cmdline": cmdline, "ppid": ppid, "owner": "orphan"}


def test_kills_an_orphan_past_the_grace_window(fake_db, monkeypatch):
    monkeypatch.setattr(reconciler, "_scan_service_subprocesses", lambda: [_orphan(5001)])
    monkeypatch.setattr(proc_compat, "proc_age_s", lambda pid: reconciler.ORPHAN_GRACE_S + 1)
    killed = []
    monkeypatch.setattr(reconciler, "_kill", lambda pid: (killed.append(pid), True)[-1])

    stats = reconciler._reconcile_once(fake_db)

    assert killed == [5001]
    assert stats["orphan_processes_killed"] == 1
    assert 5001 in reconciler._orphan_sigterm_at


def test_does_not_touch_an_orphan_younger_than_the_grace_window(fake_db, monkeypatch):
    monkeypatch.setattr(reconciler, "_scan_service_subprocesses", lambda: [_orphan(5001)])
    monkeypatch.setattr(proc_compat, "proc_age_s", lambda pid: 1.0)
    killed = []
    monkeypatch.setattr(reconciler, "_kill", lambda pid: (killed.append(pid), True)[-1])

    stats = reconciler._reconcile_once(fake_db)

    assert killed == []
    assert stats["orphan_processes_killed"] == 0


def test_skips_a_pid_that_belongs_to_a_known_running_proxy(fake_db, monkeypatch):
    # Defensive dedup: even if the process scan and the DB briefly
    # disagree, a pid a service_proxy row claims as running/starting is
    # never treated as an orphan.
    fake_db.insert_item("service_proxy", "cam-1", {"id": "cam-1", "status": "running", "pid": 5001})
    monkeypatch.setattr(process_manager, "pid_alive", lambda pid: True)
    monkeypatch.setattr(reconciler, "_scan_service_subprocesses", lambda: [_orphan(5001)])
    monkeypatch.setattr(proc_compat, "proc_age_s", lambda pid: reconciler.ORPHAN_GRACE_S + 1)
    killed = []
    monkeypatch.setattr(reconciler, "_kill", lambda pid: (killed.append(pid), True)[-1])

    reconciler._reconcile_once(fake_db)
    assert killed == []


def test_escalates_to_sigkill_once_the_sigterm_grace_window_elapses(fake_db, monkeypatch, no_orphans):
    monkeypatch.setattr(reconciler, "_scan_service_subprocesses", lambda: [_orphan(5001)])
    monkeypatch.setattr(proc_compat, "proc_age_s", lambda pid: reconciler.ORPHAN_GRACE_S + 1)
    term_calls, kill_calls = [], []
    monkeypatch.setattr(reconciler, "_kill", lambda pid: (term_calls.append(pid), True)[-1])
    monkeypatch.setattr(reconciler, "_force_kill", lambda pid: (kill_calls.append(pid), True)[-1])

    clock = {"t": 1000.0}
    monkeypatch.setattr(reconciler.time, "monotonic", lambda: clock["t"])

    # Pass 1: first sighting -> SIGTERM.
    reconciler._reconcile_once(fake_db)
    assert term_calls == [5001] and kill_calls == []

    # Pass 2: still alive, but not past the SIGKILL grace window yet.
    clock["t"] += reconciler.SIGKILL_GRACE_S - 1
    reconciler._reconcile_once(fake_db)
    assert kill_calls == []

    # Pass 3: now past it -> escalate.
    clock["t"] += 2
    stats = reconciler._reconcile_once(fake_db)
    assert kill_calls == [5001]
    assert stats["orphan_processes_force_killed"] == 1
    assert 5001 not in reconciler._orphan_sigterm_at  # cleared after escalation


def test_gcs_a_tracked_pid_once_it_stops_appearing_in_the_scan(fake_db, monkeypatch, no_orphans):
    reconciler._orphan_sigterm_at[5001] = 1.0
    reconciler._reconcile_once(fake_db)  # no_orphans -> scan returns []
    assert 5001 not in reconciler._orphan_sigterm_at


# ─── _reconcile_once: workspace ref tidy ────────────────────────────────

def test_tidies_an_orphaned_service_proxy_ids_entry(fake_db, monkeypatch, no_orphans):
    fake_db.insert_item("workspace", "w1", {"id": "w1", "kind": "user", "service_proxy_ids": ["ghost-1"]})
    stats = reconciler._reconcile_once(fake_db)
    assert fake_db.get_item("workspace", "w1")["service_proxy_ids"] == []
    assert stats["orphan_refs_tidied"] == 1


def test_tidies_an_orphaned_node_position_and_view_type(fake_db, monkeypatch, no_orphans):
    fake_db.insert_item("workspace", "w1", {
        "id": "w1", "kind": "user",
        "node_positions": {"ghost-1": {"x": 0, "y": 0}},
        "node_view_types": {"ghost-1": "view_min"},
    })
    reconciler._reconcile_once(fake_db)
    ws = fake_db.get_item("workspace", "w1")
    assert "ghost-1" not in ws["node_positions"]
    assert "ghost-1" not in ws["node_view_types"]


def test_tidies_an_orphaned_edge_endpoint(fake_db, monkeypatch, no_orphans):
    fake_db.insert_item("workspace", "w1", {
        "id": "w1", "kind": "user",
        "edges": [{"id": "e1", "source": "ghost-1", "target": "ghost-1"}],
    })
    reconciler._reconcile_once(fake_db)
    assert fake_db.get_item("workspace", "w1")["edges"] == []


def test_does_not_tidy_a_reference_to_a_real_proxy(fake_db, monkeypatch, no_orphans):
    fake_db.insert_item("service_proxy", "cam-1", {"id": "cam-1", "status": "stopped"})
    fake_db.insert_item("workspace", "w1", {"id": "w1", "kind": "user", "service_proxy_ids": ["cam-1"]})
    stats = reconciler._reconcile_once(fake_db)
    assert fake_db.get_item("workspace", "w1")["service_proxy_ids"] == ["cam-1"]
    assert stats["orphan_refs_tidied"] == 0


# ─── _scan_service_subprocesses: ownership classification ───────────────

def _fake_iter(procs):
    def iter_service_subprocesses(predicate):
        for p in procs:
            if predicate(p["cmdline"]):
                yield p
    return iter_service_subprocesses


def test_classifies_a_child_of_this_runtime_as_mine(monkeypatch):
    my_pid = os.getpid()
    proc = {"pid": 42, "cmdline": "python -m arduino_service", "ppid": my_pid}
    monkeypatch.setattr(proc_compat, "iter_service_subprocesses", _fake_iter([proc]))
    out = reconciler._scan_service_subprocesses()
    assert out == [{"pid": 42, "cmdline": proc["cmdline"], "ppid": my_pid, "owner": "mine"}]


def test_skips_a_subprocess_owned_by_another_live_backend(monkeypatch):
    proc = {"pid": 42, "cmdline": "python -m arduino_service", "ppid": 7}
    monkeypatch.setattr(proc_compat, "iter_service_subprocesses", _fake_iter([proc]))
    monkeypatch.setattr(proc_compat, "is_backend_pid", lambda ppid: True)
    assert reconciler._scan_service_subprocesses() == []


def test_classifies_as_orphan_when_parent_is_neither_self_nor_a_backend(monkeypatch):
    proc = {"pid": 42, "cmdline": "python -m arduino_service", "ppid": 1}  # reparented to init
    monkeypatch.setattr(proc_compat, "iter_service_subprocesses", _fake_iter([proc]))
    monkeypatch.setattr(proc_compat, "is_backend_pid", lambda ppid: False)
    out = reconciler._scan_service_subprocesses()
    assert out == [{"pid": 42, "cmdline": proc["cmdline"], "ppid": 1, "owner": "orphan"}]


def test_skips_a_process_with_no_ppid_yet(monkeypatch):
    proc = {"pid": 42, "cmdline": "python -m arduino_service", "ppid": None}
    monkeypatch.setattr(proc_compat, "iter_service_subprocesses", _fake_iter([proc]))
    assert reconciler._scan_service_subprocesses() == []


@pytest.mark.parametrize("cmdline,expected", [
    ("python -m arduino_service", True),
    ("python -m echo_http", True),
    ("/usr/bin/python3 -m serial_service --port /dev/ttyACM0", True),
    ("python -m botgear.main", False),   # the backend itself — never a candidate
    ("vim", False),                       # unrelated process
])
def test_candidate_predicate_matches_service_subprocess_naming(monkeypatch, cmdline, expected):
    captured = {}

    def iter_service_subprocesses(predicate):
        captured["predicate"] = predicate
        return iter(())

    monkeypatch.setattr(proc_compat, "iter_service_subprocesses", iter_service_subprocesses)
    reconciler._scan_service_subprocesses()
    assert captured["predicate"](cmdline) is expected
