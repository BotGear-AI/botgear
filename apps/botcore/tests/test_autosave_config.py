# unmanaged
"""Auto-save of runtime config mutations to the active config-set yml.

Stone 4 (partial): config edits that bypass the framework's
``set_config``/``save_config`` (which already flush yml) — namely an
interactive LINK bind/delete and a SUBPROCESS config_patch — write
``service_proxy.service_config`` directly. Without an immediate yml flush
those mutations only reach disk at the next drain; an unclean restart then
loses them, because ``boot.sync`` overwrites the DB row from the (stale)
yml. These tests pin the auto-save contract:

  1. ``boot._save_one_proxy(..., prefer_live=False)`` persists the ROW's
     config (the just-mutated truth), ignoring any live Service object —
     which may not have applied the just-broadcast config_state yet.
  2. ``prefer_live=True`` (default; the drain / per-node-save path) keeps
     preferring the live object so flushed runtime state is captured.
  3. ``link_service._persist_config`` and ``discovery._handle_config_patch``
     each trigger the auto-save for the mutated proxy.
  4. ``lifecycle.autosave_service_config`` is best-effort — no DB is a no-op,
     never raises.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest
import yaml

SRC = Path(__file__).parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

REPO_DIR = Path(__file__).parent.parent / "repo"

from botcore.runtime.boot import _row_to_config_instance, _save_one_proxy  # noqa: E402
from botcore.runtime.repo import scan_repo  # noqa: E402


@pytest.fixture(scope="module")
def manifests():
    return {m.id: m for m in scan_repo(REPO_DIR.resolve())}


def _clock_row(pid: str, interval: int, status: str = "running") -> Dict[str, Any]:
    return {
        "id": pid,
        "service_meta_id": "clock@1.0.0",
        "service_config": {"interval_ms": interval},
        "status": status,
    }


def _fake_live(framework, monkeypatch, manifests, interval: int) -> None:
    """Register a fake live Service for ``clock-1`` whose config differs
    from the row, so prefer_live can be observed either way."""
    cfg = _row_to_config_instance(_clock_row("clock-1", interval), manifests["clock@1.0.0"], REPO_DIR)
    svc = types.SimpleNamespace(config=cfg, serialize_runtime_state=lambda: None)
    handle = types.SimpleNamespace(payload={"service": svc})
    monkeypatch.setattr(framework, "REGISTRY", types.SimpleNamespace(get=lambda pid: handle))


# ─── prefer_live primitive ────────────────────────────────────────────


def test_prefer_live_false_saves_row_config(tmp_path, manifests):
    desired = _save_one_proxy(_clock_row("clock-1", 500), manifests, tmp_path, None, REPO_DIR, prefer_live=False)
    assert desired == "running"
    out = yaml.safe_load((tmp_path / "clock-1.yml").read_text())
    assert out["type"] == "clock@1.0.0"
    assert out["interval_ms"] == 500
    assert out["desired_state"] == "running"


def test_prefer_live_false_ignores_live_service(tmp_path, manifests, monkeypatch):
    from botcore import framework
    _fake_live(framework, monkeypatch, manifests, interval=999)
    _save_one_proxy(_clock_row("clock-1", 500), manifests, tmp_path, None, REPO_DIR, prefer_live=False)
    out = yaml.safe_load((tmp_path / "clock-1.yml").read_text())
    assert out["interval_ms"] == 500  # the ROW, not the live 999


def test_prefer_live_true_saves_live_service(tmp_path, manifests, monkeypatch):
    from botcore import framework
    _fake_live(framework, monkeypatch, manifests, interval=999)
    _save_one_proxy(_clock_row("clock-1", 500), manifests, tmp_path, None, REPO_DIR, prefer_live=True)
    out = yaml.safe_load((tmp_path / "clock-1.yml").read_text())
    assert out["interval_ms"] == 999  # live object wins (captures runtime state)


# ─── mutation-site wiring ─────────────────────────────────────────────


class FakeDB:
    def __init__(self) -> None:
        self.t: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def get_item(self, table: str, key: str) -> Optional[Dict[str, Any]]:
        return self.t.get((table, key))

    def insert_item(self, table: str, key: str, row: Dict[str, Any]) -> None:
        self.t[(table, key)] = dict(row)

    def update_item(self, table: str, key: str, row: Dict[str, Any], *, include_nulls: bool = True) -> None:
        self.t[(table, key)] = dict(row)


def test_link_persist_config_writes_yml(monkeypatch):
    from botcore.runtime import lifecycle
    from botcore.services import link_service

    saved = []
    monkeypatch.setattr(lifecycle, "save_proxy_config", lambda pid, cfg: saved.append((pid, cfg)) or True)
    db = FakeDB()
    link_service._persist_config(db, "servo-1", {"id": "servo-1"}, {"controller_id": "arduino-1"})

    assert saved == [("servo-1", {"controller_id": "arduino-1"})]


def test_config_patch_writes_yml(monkeypatch):
    from botcore.runtime import discovery, lifecycle

    saved = []
    monkeypatch.setattr(lifecycle, "proxy_config", lambda pid: {})
    monkeypatch.setattr(lifecycle, "save_proxy_config", lambda pid, cfg: saved.append((pid, cfg)) or True)
    db = FakeDB()
    db.insert_item("service_proxy", "servo-1", {"id": "servo-1"})
    monkeypatch.setattr(discovery, "get_database_client", lambda: db)

    discovery._handle_config_patch("/service_proxy/servo-1/config_patch", {"angle": 90})

    assert saved == [("servo-1", {"angle": 90})]


# ─── best-effort helper ───────────────────────────────────────────────


def test_save_proxy_config_no_db_is_noop(monkeypatch):
    from botcore.runtime import lifecycle
    monkeypatch.setattr(lifecycle, "get_database_client", lambda: None)
    assert lifecycle.save_proxy_config("anything", {"x": 1}) is False
