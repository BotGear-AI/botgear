# unmanaged
"""system.drain_for_exit + event_handlers.on_shutdown — every way the
backend ends stops its services first, so no subprocess outlives it."""
from __future__ import annotations

import pytest

from botgear import event_handlers
from botgear.runtime import lifecycle, process_manager, system


@pytest.fixture
def calls(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(lifecycle, "save_all_service_config",
                        lambda: seen.append("snapshot") or {"saved": 2})
    monkeypatch.setattr(process_manager, "stop_all", lambda: seen.append("stop_all") or 2)
    monkeypatch.setattr(lifecycle, "drain_services", lambda: seen.append("drain") or 2)
    monkeypatch.setattr(system, "_DRAINING", False)
    return seen


def test_snapshot_then_stop_subprocesses_then_drain(calls):
    assert system.drain_for_exit("test") == 2
    assert calls == ["snapshot", "stop_all", "drain"]
    assert system.is_draining()


def test_failures_never_block_exit(calls, monkeypatch):
    def boom():
        raise RuntimeError("db gone")
    monkeypatch.setattr(lifecycle, "save_all_service_config", boom)
    monkeypatch.setattr(process_manager, "stop_all", boom)
    monkeypatch.setattr(lifecycle, "drain_services", boom)
    assert system.drain_for_exit("test") == 0


def test_graceful_paths_drain_before_ending(calls, monkeypatch):
    ended: list[str] = []
    monkeypatch.setattr(system, "restart", lambda: ended.append("restart"))
    monkeypatch.setattr(system, "shutdown", lambda: ended.append("shutdown"))
    system.graceful_restart()
    system.graceful_shutdown()
    assert ended == ["restart", "shutdown"]
    assert calls.count("drain") == 2


def test_on_shutdown_drains_without_snapshotting(calls):
    # A plain exit must not rewrite ymls from in-memory config: that
    # reverted a hand-edited agent.yml (key + residency) on restart.
    event_handlers.on_shutdown()
    assert calls == ["stop_all", "drain"]


def test_on_shutdown_skips_when_already_draining(calls, monkeypatch):
    monkeypatch.setattr(system, "_DRAINING", True)
    event_handlers.on_shutdown()
    assert calls == []


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="POSIX signal semantics")
def test_sigterm_runs_on_shutdown_then_still_dies_by_sigterm(tmp_path):
    """uvicorn restores the pre-serve SIGTERM handler and re-raises the
    signal on exit — the handler installed at startup must drain, then let
    the process die with SIGTERM's usual status."""
    import signal
    import subprocess
    import sys
    marker = tmp_path / "drained"
    script = f"""
import os, signal
from botgear import event_handlers
event_handlers.on_shutdown = lambda: open({str(marker)!r}, "w").write("x")
event_handlers._install_sigterm_handler()
os.kill(os.getpid(), signal.SIGTERM)
raise SystemExit("not reached: SIGTERM should have ended the process")
"""
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert marker.exists(), proc.stderr[-2000:]
    assert proc.returncode == -signal.SIGTERM
