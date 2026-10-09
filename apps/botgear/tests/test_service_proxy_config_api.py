# unmanaged
"""Tests for POST /v1/service-proxy/{id}/config — the yml-aware config
save that replaces the Composer's old raw ``PUT /service-proxy/{id}``
(which only wrote the service_proxy row's service_config, a mirror a
subprocess service never reads — see service_proxy_config_api.py's
docstring). Calls the route function directly (bypassing FastAPI/auth)
with a fake DB, matching test_lifecycle.py's FakeDB pattern.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from botgear.api import service_proxy_config_api as api
from botgear.runtime import lifecycle


class FakeDB:
    def __init__(self) -> None:
        self.tables: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def _table(self, name: str) -> Dict[str, Dict[str, Any]]:
        return self.tables.setdefault(name, {})

    def get_item(self, table: str, key: str) -> Optional[Dict[str, Any]]:
        return self._table(table).get(key)

    def update_item(self, table: str, key: str, item: Dict[str, Any], include_nulls: bool = False) -> None:
        self._table(table)[key] = dict(item)


@pytest.fixture
def db(monkeypatch) -> FakeDB:
    fake = FakeDB()
    monkeypatch.setattr("database.factory.get_database_client", lambda: fake)
    return fake


def test_save_merges_onto_current_config_and_writes_yml(db, monkeypatch):
    db.update_item("service_proxy", "oakd-1", {"id": "oakd-1", "configured": False})

    monkeypatch.setattr(lifecycle, "proxy_config", lambda pid: {"depth_enabled": True, "publish_feature_flow": True})
    written = {}

    def fake_save(pid: str, cfg: Dict[str, Any]) -> bool:
        written[pid] = cfg
        return True

    monkeypatch.setattr(lifecycle, "save_proxy_config", fake_save)

    result = api.save_proxy_config(
        "oakd-1",
        api.SaveProxyConfigRequest(config={"depth_align_to_rgb": True, "publish_feature_flow": False}),
        None,
    )

    assert result.ok is True
    # Merge, not replace: depth_enabled (untouched) survives alongside
    # the two fields the wizard actually changed.
    assert written["oakd-1"] == {
        "depth_enabled": True,
        "publish_feature_flow": False,
        "depth_align_to_rgb": True,
    }
    # First-ever save marks the instance configured (config gate won't re-prompt).
    assert db.get_item("service_proxy", "oakd-1")["configured"] is True


def test_save_leaves_configured_flag_alone_once_already_set(db, monkeypatch):
    db.update_item("service_proxy", "slam-1", {"id": "slam-1", "configured": True})
    monkeypatch.setattr(lifecycle, "proxy_config", lambda pid: {})
    monkeypatch.setattr(lifecycle, "save_proxy_config", lambda pid, cfg: True)

    api.save_proxy_config("slam-1", api.SaveProxyConfigRequest(config={"camera_tilt_deg": 10}), None)

    # update_item shouldn't have been called again to flip a flag that was
    # already true — the row in the fake DB should be untouched otherwise.
    assert db.get_item("service_proxy", "slam-1") == {"id": "slam-1", "configured": True}


def test_unknown_proxy_returns_404(db, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(lifecycle, "proxy_config", lambda pid: {})
    monkeypatch.setattr(lifecycle, "save_proxy_config", lambda pid, cfg: True)

    with pytest.raises(HTTPException) as exc_info:
        api.save_proxy_config("nonexistent", api.SaveProxyConfigRequest(config={}), None)
    assert exc_info.value.status_code == 404


def test_get_returns_yml_resolved_config_not_the_db_mirror(db, monkeypatch):
    # The DB row's service_config is a stale mirror for subprocess types
    # (see module docstring) — GET must reflect lifecycle.proxy_config
    # (yml/live), not whatever (if anything) sits in the row.
    db.update_item("service_proxy", "oakd-1", {
        "id": "oakd-1",
        "service_config": {"mono_stream_camera": None},  # stale mirror
    })
    monkeypatch.setattr(
        lifecycle, "proxy_config",
        lambda pid: {"mono_stream_camera": "right", "filters": [{"type": "overlay"}]},
    )

    result = api.get_proxy_config("oakd-1", None)

    assert result.config == {"mono_stream_camera": "right", "filters": [{"type": "overlay"}]}


def test_get_unknown_proxy_returns_404(db, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(lifecycle, "proxy_config", lambda pid: {})

    with pytest.raises(HTTPException) as exc_info:
        api.get_proxy_config("nonexistent", None)
    assert exc_info.value.status_code == 404
