# unmanaged
"""Unit tests for config_set_service — the filesystem-backed config-set
resource (GET /v1/config-sets[/{id}], POST /v1/config-sets-request).

Everything here operates on a real tmp_path tree (sets are directories of
.yml files) rather than mocking the filesystem — truer to what the module
actually does (os.path/shutil calls) and most of its logic IS the
filesystem walk.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from botgear.runtime import config_sets
from botgear.services import config_set_service as svc


@pytest.fixture
def sets_root(tmp_path, monkeypatch):
    """<tmp>/data/config_sets/, with a 'default' set already on disk and
    pinned as the active (booted) set. SET_ENV_VAR cleared so the pending
    set resolves from the marker file (or falls back to default), not
    whatever's in the real process environment."""
    data_dir = tmp_path / "data"
    root = data_dir / "config_sets"
    (root / "default").mkdir(parents=True)
    monkeypatch.setattr(svc, "get_settings", lambda: SimpleNamespace(data_dir=str(data_dir)))
    monkeypatch.setattr(config_sets, "_BOOTED_SET", "default")
    monkeypatch.delenv(config_sets.SET_ENV_VAR, raising=False)
    return root


def _mk_set(root, name, *proxy_ymls: tuple, runtime_yml: str | None = None):
    """Creates <root>/<name>/ with the given (filename, content) proxy ymls
    and an optional runtime.yml body."""
    set_dir = root / name
    set_dir.mkdir(parents=True, exist_ok=True)
    for fname, content in proxy_ymls:
        (set_dir / fname).write_text(content)
    if runtime_yml is not None:
        (set_dir / "runtime.yml").write_text(runtime_yml)
    return set_dir


# ─── get_all_config_set ────────────────────────────────────────────────

def test_get_all_lists_every_set_with_active_and_pending_flags(sets_root):
    _mk_set(sets_root, "staging", ("cam-1.yml", "type: stub@1.0.0\n"))
    out = svc.get_all_config_set(user={}, request=None)

    by_id = {row["id"]: row for row in out}
    assert set(by_id) == {"default", "staging"}
    assert by_id["default"]["active"] is True
    assert by_id["default"]["pending"] is True  # no marker yet -> pending == active == default
    assert by_id["staging"]["active"] is False
    assert by_id["staging"]["proxy_count"] == 1


def test_get_all_counts_proxy_ymls_and_ignores_runtime_yml_and_dotfiles(sets_root):
    _mk_set(
        sets_root, "default",
        ("cam-1.yml", "type: stub@1.0.0\n"),
        ("arm-1.yml", "type: stub@1.0.0\n"),
        (".hidden.yml", "type: stub@1.0.0\n"),
        runtime_yml="registry: [cam-1, arm-1]\n",
    )
    out = svc.get_all_config_set(user={}, request=None)
    default = next(r for r in out if r["id"] == "default")
    assert default["proxy_count"] == 2
    assert default["has_runtime_yml"] is True


def test_get_all_surfaces_the_active_set_even_if_its_folder_is_missing(sets_root, monkeypatch):
    # The booted set's folder was removed out from under the process
    # (operator deleted it externally) — still shows up as the active row.
    monkeypatch.setattr(config_sets, "_BOOTED_SET", "vanished")
    out = svc.get_all_config_set(user={}, request=None)
    vanished = next(r for r in out if r["id"] == "vanished")
    assert vanished["active"] is True
    assert vanished["proxy_count"] == 0


def test_pending_set_name_honors_the_env_var_over_the_marker(sets_root, monkeypatch):
    _mk_set(sets_root, "staging")
    svc.process_config_set_request({"action": "switch", "name": "staging"}, user={}, request=None)
    monkeypatch.setenv(config_sets.SET_ENV_VAR, "default")
    out = svc.get_all_config_set(user={}, request=None)
    # Marker says "staging" but the env var wins.
    assert next(r for r in out if r["id"] == "default")["pending"] is True
    assert next(r for r in out if r["id"] == "staging")["pending"] is False


def test_scan_proxies_flags_a_non_mapping_yml_as_a_parse_error(sets_root):
    _mk_set(sets_root, "default", ("weird.yml", "- just\n- a\n- list\n"), runtime_yml="registry: []\n")
    out = svc.get_config_set("default", user={}, request=None)
    weird = next(c for c in out["candidates"] if c["proxy_id"] == "weird")
    assert weird["parse_error"] == "not a yaml mapping"


def test_scan_proxies_flags_a_yaml_syntax_error(sets_root):
    _mk_set(sets_root, "default", ("broken.yml", "type: [unclosed\n"), runtime_yml="registry: []\n")
    out = svc.get_config_set("default", user={}, request=None)
    broken = next(c for c in out["candidates"] if c["proxy_id"] == "broken")
    assert broken["parse_error"] is not None
    assert broken["type_id"] is None


def test_get_all_synthesizes_a_default_entry_when_the_root_is_absent(tmp_path, monkeypatch):
    # No config_sets/ dir at all yet (very first boot).
    data_dir = tmp_path / "data"
    monkeypatch.setattr(svc, "get_settings", lambda: SimpleNamespace(data_dir=str(data_dir)))
    monkeypatch.setattr(config_sets, "_BOOTED_SET", "default")
    out = svc.get_all_config_set(user={}, request=None)
    assert len(out) == 1
    assert out[0]["id"] == "default"
    assert out[0]["proxy_count"] == 0


# ─── get_config_set ─────────────────────────────────────────────────────

def test_get_config_set_returns_none_for_missing_set(sets_root):
    assert svc.get_config_set("ghost", user={}, request=None) is None


def test_get_config_set_rejects_an_unsafe_name(sets_root):
    with pytest.raises(HTTPException) as exc_info:
        svc.get_config_set("../etc", user={}, request=None)
    assert exc_info.value.status_code == 400


def test_get_config_set_splits_ordered_proxies_from_candidates(sets_root):
    _mk_set(
        sets_root, "default",
        ("cam-1.yml", "type: stub@1.0.0\ndesired_state: running\n"),
        ("parked-1.yml", "type: stub@1.0.0\n"),  # not in start_order -> candidate
        runtime_yml="registry: [cam-1]\n",
    )
    out = svc.get_config_set("default", user={}, request=None)
    assert out["start_order"] == ["cam-1"]
    assert [p["proxy_id"] for p in out["proxies"]] == ["cam-1"]
    assert out["proxies"][0]["desired_state"] == "running"
    assert [c["proxy_id"] for c in out["candidates"]] == ["parked-1"]
    assert out["active"] is True


def test_get_config_set_flags_a_missing_yml_for_a_registered_proxy(sets_root):
    # "ghost-1" is in the registry but its yml was deleted/never written.
    _mk_set(sets_root, "default", runtime_yml="registry: [ghost-1]\n")
    out = svc.get_config_set("default", user={}, request=None)
    assert out["proxies"][0]["proxy_id"] == "ghost-1"
    assert out["proxies"][0]["parse_error"] == "missing yml file"


# ─── process_config_set_request: switch ────────────────────────────────

def test_switch_writes_the_marker_and_reports_restart_required(sets_root):
    _mk_set(sets_root, "staging")
    out = svc.process_config_set_request({"action": "switch", "name": "staging"}, user={}, request=None)
    assert out["metadata"] == {
        "status": "success", "action": "switch", "active": "staging",
        "previous_active": "default", "restart_required": True,
        "note": "Set takes effect on next backend restart.",
    }
    assert (sets_root / svc.ACTIVE_MARKER).read_text() == "staging"
    # The records in the response reflect the marker (pending), not yet active.
    staging_row = next(r for r in out["records"] if r["id"] == "staging")
    assert staging_row["pending"] is True
    assert staging_row["active"] is False


def test_switch_to_a_missing_set_raises_404(sets_root):
    with pytest.raises(HTTPException) as exc_info:
        svc.process_config_set_request({"action": "switch", "name": "ghost"}, user={}, request=None)
    assert exc_info.value.status_code == 404


# ─── process_config_set_request: duplicate ──────────────────────────────

def test_duplicate_copies_the_tree_under_the_new_name(sets_root):
    _mk_set(sets_root, "default", ("cam-1.yml", "type: stub@1.0.0\n"), runtime_yml="registry: [cam-1]\n")
    out = svc.process_config_set_request(
        {"action": "duplicate", "name": "default", "new_name": "default-copy"}, user={}, request=None,
    )
    assert out["metadata"]["status"] == "success"
    copy_dir = sets_root / "default-copy"
    assert (copy_dir / "cam-1.yml").is_file()
    assert (copy_dir / "runtime.yml").is_file()


def test_duplicate_clears_a_carried_over_migrated_marker(sets_root):
    src = _mk_set(sets_root, "default")
    (src / ".migrated").write_text("x")
    svc.process_config_set_request({"action": "duplicate", "name": "default", "new_name": "copy"}, user={}, request=None)
    assert not (sets_root / "copy" / ".migrated").exists()


def test_duplicate_missing_source_raises_404(sets_root):
    with pytest.raises(HTTPException) as exc_info:
        svc.process_config_set_request({"action": "duplicate", "name": "ghost", "new_name": "x"}, user={}, request=None)
    assert exc_info.value.status_code == 404


def test_duplicate_onto_an_existing_target_raises_409(sets_root):
    _mk_set(sets_root, "staging")
    with pytest.raises(HTTPException) as exc_info:
        svc.process_config_set_request({"action": "duplicate", "name": "default", "new_name": "staging"}, user={}, request=None)
    assert exc_info.value.status_code == 409


# ─── process_config_set_request: delete ─────────────────────────────────

def test_delete_removes_the_set_directory(sets_root):
    _mk_set(sets_root, "staging")
    out = svc.process_config_set_request({"action": "delete", "name": "staging"}, user={}, request=None)
    assert out["metadata"] == {"status": "success", "action": "delete", "deleted": "staging"}
    assert not (sets_root / "staging").exists()


def test_delete_refuses_the_pending_set(sets_root):
    _mk_set(sets_root, "staging")
    svc.process_config_set_request({"action": "switch", "name": "staging"}, user={}, request=None)
    with pytest.raises(HTTPException) as exc_info:
        svc.process_config_set_request({"action": "delete", "name": "staging"}, user={}, request=None)
    assert exc_info.value.status_code == 409


def test_delete_refuses_the_default_set_even_when_not_pending(sets_root):
    # Switch pending away from "default" first, so the ONLY reason left
    # for delete to refuse is the hardcoded DEFAULT_SET_NAME guard.
    _mk_set(sets_root, "staging")
    svc.process_config_set_request({"action": "switch", "name": "staging"}, user={}, request=None)
    with pytest.raises(HTTPException) as exc_info:
        svc.process_config_set_request({"action": "delete", "name": "default"}, user={}, request=None)
    assert exc_info.value.status_code == 409


def test_delete_missing_set_raises_404(sets_root):
    with pytest.raises(HTTPException) as exc_info:
        svc.process_config_set_request({"action": "delete", "name": "ghost"}, user={}, request=None)
    assert exc_info.value.status_code == 404


# ─── process_config_set_request: validation ─────────────────────────────

def test_unknown_action_raises_400(sets_root):
    with pytest.raises(HTTPException) as exc_info:
        svc.process_config_set_request({"action": "nuke", "name": "default"}, user={}, request=None)
    assert exc_info.value.status_code == 400


def test_unsafe_name_raises_400_before_touching_disk(sets_root):
    with pytest.raises(HTTPException) as exc_info:
        svc.process_config_set_request({"action": "delete", "name": "../../etc"}, user={}, request=None)
    assert exc_info.value.status_code == 400
