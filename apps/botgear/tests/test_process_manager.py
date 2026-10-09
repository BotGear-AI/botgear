# unmanaged
"""Unit tests for process_manager — real subprocess supervision.

Two tiers:
  * Pure helpers (_substitute, _allocate_port, pid_alive, the topic
    name formatters) — no subprocess needed.
  * start()/stop()/stop_all()/the crash watcher — these fundamentally
    manage real OS processes, so rather than mock subprocess.Popen's
    whole surface (poll/returncode/stdout/stderr/wait — a lot to get
    right and easy to get subtly wrong), these spawn short-lived real
    `python -c "..."` children. Timeouts are kept small (stop()'s
    `timeout` kwarg) so the suite stays fast.

get_bus() is monkeypatched to a recording fake for these tests (unlike
reconciler's/lifecycle's tests, which share the real one) — the crash
watcher's retained lifecycle publish and the stdout/stderr pumps' log
publishes are exactly what several tests assert on, and a fake sidesteps
needing to synchronize with the real bus's subscriber dispatch.
"""
from __future__ import annotations

import sys
import threading
import time

import pytest

from botgear.runtime import process_manager as pm
from botgear.runtime import proc_compat


class FakeBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, object, bool]] = []

    def publish_sync(self, topic, payload, *, retained=False, **_kw):
        self.published.append((topic, payload, retained))


@pytest.fixture(autouse=True)
def _clean_registry():
    """_REGISTRY is module-global — make sure a test that spawns a real
    process always gets it stopped + deregistered, even on failure/skip,
    so no process (or thread) leaks into a later test."""
    yield
    with pm._REGISTRY_LOCK:
        leftover = list(pm._REGISTRY.keys())
    for proxy_id in leftover:
        pm.stop(proxy_id, timeout=0.5)


@pytest.fixture
def fake_bus(monkeypatch):
    bus = FakeBus()
    monkeypatch.setattr(pm, "get_bus", lambda: bus)
    return bus


def _sleep_argv(seconds: float = 5.0) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


# ─── pure helpers ───────────────────────────────────────────────────────

def test_substitute_replaces_every_occurrence_of_each_placeholder():
    out = pm._substitute(
        ["run", "--port=${PORT}", "--host=${HOST}", "${PORT}-${PORT}"],
        {"PORT": "9000", "HOST": "127.0.0.1"},
    )
    assert out == ["run", "--port=9000", "--host=127.0.0.1", "9000-9000"]


def test_substitute_leaves_unmatched_tokens_alone():
    assert pm._substitute(["plain", "${UNKNOWN}"], {"PORT": "9000"}) == ["plain", "${UNKNOWN}"]


def test_allocate_port_returns_a_usable_distinct_port_each_call():
    a = pm._allocate_port()
    b = pm._allocate_port()
    assert 0 < a < 65536
    assert 0 < b < 65536
    assert a != b  # not a hard guarantee from the OS, but overwhelmingly true


def test_pid_alive_delegates_to_proc_compat(monkeypatch):
    monkeypatch.setattr(proc_compat, "pid_alive", lambda pid: pid == 42)
    assert pm.pid_alive(42) is True
    assert pm.pid_alive(43) is False


def test_topic_formatters():
    assert pm._log_topic("cam-1") == "/service_proxy/cam-1/log"
    assert pm._lifecycle_topic("cam-1") == "/service_proxy/cam-1/lifecycle"


def test_is_running_false_for_an_unknown_proxy():
    assert pm.is_running("never-started") is False


# ─── start() / is_running() / stop() — real short-lived subprocesses ────

def test_start_reports_a_live_pid_and_registers_it(tmp_path):
    out = pm.start("p1", _sleep_argv(), tmp_path, port=9500, cwd=tmp_path)
    assert out["pid"] > 0
    assert out["port"] == 9500
    assert out["host"] == "127.0.0.1"
    assert pm.is_running("p1") is True
    assert proc_compat.pid_alive(out["pid"]) is True


def test_start_is_idempotent_on_an_already_running_proxy(tmp_path):
    first = pm.start("p1", _sleep_argv(), tmp_path, port=9501, cwd=tmp_path)
    second = pm.start("p1", _sleep_argv(), tmp_path, port=9999, cwd=tmp_path)
    assert second == first  # same pid/port — the second argv was never spawned


def test_start_resolves_a_bare_python_argv0_to_the_bin_dirs_interpreter(tmp_path):
    # entry_argv commonly reads just "python" (portable across hosts) —
    # start() must resolve that against bin_dir's own interpreter rather
    # than whatever's first on PATH, so it picks up the type's own venv.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "python"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    wrapper.chmod(0o755)

    out = pm.start("p1", ["python", "-c", "print('ok')"], bin_dir, port=9511, cwd=tmp_path)
    assert out["pid"] > 0
    pm.stop("p1")


def test_start_requires_a_non_empty_argv(tmp_path):
    with pytest.raises(ValueError):
        pm.start("p1", [], tmp_path)


def test_stop_terminates_the_process_and_deregisters_it(tmp_path):
    out = pm.start("p1", _sleep_argv(), tmp_path, port=9502, cwd=tmp_path)
    result = pm.stop("p1")
    assert result["stopped"] is True
    assert not proc_compat.pid_alive(out["pid"])
    assert pm.is_running("p1") is False


def test_stop_on_an_unknown_proxy_is_a_no_op_success():
    assert pm.stop("never-started") == {"stopped": True, "rc": None}


def test_stop_on_an_already_exited_process_returns_promptly(tmp_path):
    # A real short-lived process races the crash-watcher thread (it polls
    # every 0.5s and would pop the registry entry itself the moment it
    # notices the exit — see test_crash_watcher_* below). To hit stop()'s
    # own "already exited" branch (process.poll() is not None,
    # lines 377-381) deterministically rather than racing that thread, a
    # fake _Running with a stub Popen that's already exited is inserted
    # directly, with no watcher thread at all.
    class _ExitedPopen:
        pid = 12345
        returncode = 0
        def poll(self):
            return 0

    running = pm._Running(
        proxy_id="p1", process=_ExitedPopen(), port=9503, host="127.0.0.1",
        bin_dir=tmp_path, log_topic="/service_proxy/p1/log", pumps=[],
        watcher=threading.Thread(), expected_stop=threading.Event(),
    )
    with pm._REGISTRY_LOCK:
        pm._REGISTRY["p1"] = running

    result = pm.stop("p1")
    assert result == {"stopped": True, "rc": 0}
    assert pm.is_running("p1") is False


def test_stop_escalates_to_sigkill_when_the_graceful_request_has_no_effect(tmp_path, monkeypatch):
    # A real SIGTERM-ignoring child races its own handler-install against
    # the signal, which is flaky under load. Simulating "graceful request
    # had no effect" by making it a no-op is deterministic and exercises
    # the exact same stop() branch (process still alive past `timeout`).
    monkeypatch.setattr(proc_compat, "request_graceful_stop", lambda pid: None)
    out = pm.start("p1", _sleep_argv(30), tmp_path, port=9504, cwd=tmp_path)
    result = pm.stop("p1", timeout=0.3)  # short graceful window -> forces the SIGKILL path
    assert result["stopped"] is True
    assert not proc_compat.pid_alive(out["pid"])


def test_stop_all_stops_every_registered_process(tmp_path):
    pm.start("p1", _sleep_argv(), tmp_path, port=9505, cwd=tmp_path)
    pm.start("p2", _sleep_argv(), tmp_path, port=9506, cwd=tmp_path)
    pm.stop_all()
    assert pm.is_running("p1") is False
    assert pm.is_running("p2") is False


def test_stop_all_shares_one_deadline_and_kills_sigterm_ignorers(tmp_path, monkeypatch):
    # Three services that ignore the graceful request: one-by-one stop()
    # would wait the timeout three times; stop_all waits it once.
    monkeypatch.setattr(proc_compat, "request_graceful_stop", lambda pid: None)
    pids = [pm.start(f"p{i}", _sleep_argv(30), tmp_path, port=9510 + i, cwd=tmp_path)["pid"]
            for i in range(3)]
    t0 = time.monotonic()
    assert pm.stop_all(timeout=0.5) == 3
    assert time.monotonic() - t0 < 1.4
    assert not any(proc_compat.pid_alive(p) for p in pids)
    assert pm._REGISTRY == {}


# ─── stdout/stderr pumping ───────────────────────────────────────────────

def test_stdout_is_pumped_to_the_log_topic(tmp_path, fake_bus):
    pm.start("p1", [sys.executable, "-c", "print('hello-from-child')"], tmp_path, port=9507, cwd=tmp_path)
    deadline = time.monotonic() + 3.0
    lines = []
    while time.monotonic() < deadline:
        lines = [p for (topic, p, _r) in fake_bus.published if topic == "/service_proxy/p1/log"]
        if lines:
            break
        time.sleep(0.05)
    assert any(entry["line"] == "hello-from-child" and entry["stream"] == "stdout" for entry in lines)


# ─── crash watcher ────────────────────────────────────────────────────

def test_crash_watcher_marks_an_unexpected_exit_as_error(tmp_path, fake_bus, monkeypatch):
    # No DB configured for this test — the watcher's DB update is
    # best-effort, so it should still publish + deregister without it.
    monkeypatch.setattr("database.factory.get_database_client", lambda: None)
    pm.start("p1", [sys.executable, "-c", "import sys; sys.exit(7)"], tmp_path, port=9508, cwd=tmp_path)

    deadline = time.monotonic() + 3.0
    while pm.is_running("p1") and time.monotonic() < deadline:
        time.sleep(0.05)
    # The watcher's own poll loop runs every 0.5s — give it one more tick
    # to publish + deregister after the process itself has exited.
    time.sleep(0.6)

    assert pm.is_running("p1") is False
    lifecycle_events = [p for (topic, p, retained) in fake_bus.published if topic == "/service_proxy/p1/lifecycle"]
    assert lifecycle_events and lifecycle_events[-1]["status"] == "error"


def test_crash_watcher_persists_the_error_onto_the_service_proxy_row(tmp_path, fake_bus, fake_db, monkeypatch):
    monkeypatch.setattr("database.factory.get_database_client", lambda: fake_db)
    fake_db.insert_item("service_proxy", "p1", {"id": "p1", "status": "running", "pid": 1, "port": 9510})

    pm.start("p1", [sys.executable, "-c", "import sys; sys.exit(7)"], tmp_path, port=9510, cwd=tmp_path)
    deadline = time.monotonic() + 3.0
    while pm.is_running("p1") and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.6)  # let the watcher's own poll tick persist + publish

    row = fake_db.get_item("service_proxy", "p1")
    assert row["status"] == "error"
    assert row["pid"] is None
    assert row["port"] is None
    assert row["error"] == "process exited unexpectedly (rc=7)"
    assert row["stopped_at"]


def test_crash_watcher_does_nothing_when_stop_drove_the_exit(tmp_path, fake_bus):
    # A normal stop() sets expected_stop before the process dies, so the
    # watcher must NOT also publish an "error" lifecycle event.
    pm.start("p1", _sleep_argv(), tmp_path, port=9509, cwd=tmp_path)
    pm.stop("p1")
    time.sleep(0.6)  # let the watcher thread's poll loop notice the exit
    lifecycle_events = [p for (topic, p, retained) in fake_bus.published if topic == "/service_proxy/p1/lifecycle"]
    assert lifecycle_events == []
