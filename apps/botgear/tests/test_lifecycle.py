# unmanaged
"""Unit tests for the service lifecycle state machine.

Uses an in-memory dict-backed fake DatabaseAdapter so we don't have to
spin up TinyDB. The lifecycle module only depends on the small
``get_database_client`` + ``DatabaseAdapter`` surface, which makes
substitution clean.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from botgear.runtime import lifecycle


class FakeDB:
    """Minimal in-memory DatabaseAdapter substitute for lifecycle tests."""

    def __init__(self) -> None:
        self.tables: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def _table(self, name: str) -> Dict[str, Dict[str, Any]]:
        return self.tables.setdefault(name, {})

    def insert_item(self, table: str, key: str, item: Dict[str, Any]) -> None:
        self._table(table)[key] = dict(item)

    def update_item(self, table: str, key: str, item: Dict[str, Any], include_nulls: bool = False) -> None:
        # Matches the real DatabaseAdapter signature. The FakeDB always
        # replaces, so include_nulls doesn't change behavior here — it
        # just makes the kwarg legal.
        self._table(table)[key] = dict(item)

    def get_item(self, table: str, key: str) -> Optional[Dict[str, Any]]:
        return self._table(table).get(key)

    def delete_item(self, table: str, key: str) -> None:
        self._table(table).pop(key, None)

    def get_all_items(self, table: str) -> list:
        return list(self._table(table).values())


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(
        "botgear.runtime.lifecycle.get_database_client",
        lambda: fake,
    )
    # Seed one service_meta. Uses a fake type name that doesn't match
    # any real builtin so the lifecycle dispatch lands in our stub
    # runner (registered below) instead of spawning a real thread.
    fake.insert_item(
        "service_meta",
        "stub@1.0.0",
        {"id": "stub@1.0.0", "name": "stub", "version": "1.0.0"},
    )

    # Register a stub ServiceAdapter for the "stub" service type. The
    # framework's pick_adapter is the single dispatch point lifecycle
    # talks to; swapping it for a stub keeps the test pure (no threads,
    # no asyncio) while still exercising the real lifecycle state
    # machine.
    counter = {"n": 0}

    from botgear import framework
    from botgear.framework.adapter import ServiceAdapter, ServiceHandle

    class _StubAdapter(ServiceAdapter):
        def transport_name(self) -> str:
            return "stub"

        def start(self, proxy, meta, config):
            counter["n"] += 1
            return ServiceHandle(
                proxy_id=proxy["id"],
                transport="stub",
                pid=900000 + counter["n"],
                host="stub",
                port=None,
            )

        def stop(self, handle):
            return None

    # Lifecycle now consults process_manager.pid_alive to bypass
    # "refuse to uninstall running" when the recorded PID is dead.
    # The stub's synthetic PIDs aren't real OS processes, so without
    # this patch every test that goes through running → uninstall
    # would silently take the bypass path. Pretend everything's alive
    # so the tests exercise the canonical state machine.
    monkeypatch.setattr(
        "botgear.runtime.process_manager.pid_alive",
        lambda pid: pid is not None and pid > 0,
    )

    stub_adapter = _StubAdapter()
    monkeypatch.setattr(framework, "pick_adapter", lambda meta: stub_adapter)

    return fake


def _req(action: str, **kw) -> Dict[str, Any]:
    return {"id": f"req-{action}-{kw.get('proxy_name') or kw.get('service_proxy_id') or ''}", "action": action, **kw}


def test_install_creates_proxy(db):
    out = lifecycle.handle(
        _req("install", service_meta_id="stub@1.0.0", proxy_name="echo-1")
    )
    assert out["status"] == "completed"
    proxy = db.get_item("service_proxy", "echo-1")
    assert proxy is not None
    assert proxy["status"] == "installed"
    assert proxy["service_meta_id"] == "stub@1.0.0"


@pytest.mark.parametrize("name", [
    "sabertooth-1", "network_scanner-1", "a", "cam-front_2", "a" * 63,
])
def test_install_accepts_valid_names(db, name):
    out = lifecycle.handle(_req("install", service_meta_id="stub@1.0.0", proxy_name=name))
    assert out["status"] == "completed"
    assert db.get_item("service_proxy", name) is not None


@pytest.mark.parametrize("name", [
    "a/b", "../x", "a\\b", "cam+1", "cam#", "foo@bar", "my service",
    "workE", "Cam-1", "1cam", "-cam", "_cam", ".hidden", "a.b", "a?b", "a%20b",
    "a" * 64,
])
def test_install_rejects_invalid_names(db, name):
    out = lifecycle.handle(_req("install", service_meta_id="stub@1.0.0", proxy_name=name))
    assert out["status"] == "failed"
    assert "invalid service name" in out["result"]
    assert db.get_item("service_proxy", name) is None


def test_install_placeholder_skips_install(db):
    # M1 decouple: dropping a service creates a placeholder — no venv, no
    # pip, no "installed" transition — until the user presses Start.
    out = lifecycle.handle(
        _req("install", service_meta_id="stub@1.0.0", proxy_name="ph-1", placeholder=True)
    )
    assert out["status"] == "completed"
    proxy = db.get_item("service_proxy", "ph-1")
    assert proxy["status"] == "placeholder"
    assert proxy.get("installed_at") is None


def test_start_from_placeholder_installs_then_runs(db):
    # Start on a placeholder runs the type install (idempotent; a no-op
    # for the builtin-shaped stub meta) and then launches the instance.
    lifecycle.handle(
        _req("install", service_meta_id="stub@1.0.0", proxy_name="ph-2", placeholder=True)
    )
    assert db.get_item("service_proxy", "ph-2")["status"] == "placeholder"
    lifecycle.handle(_req("start", service_proxy_id="ph-2"))
    proxy = db.get_item("service_proxy", "ph-2")
    assert proxy["status"] == "running"
    assert proxy.get("installed_at")


def test_start_merges_request_config(db, monkeypatch):
    # Install-wizard inputs ride on the start request's `config` and are
    # merged onto the instance's current config, then persisted to the yml
    # (stone 4b: NOT the DB row) before launch.
    saved = {}
    monkeypatch.setattr(lifecycle, "proxy_config", lambda pid: {})
    monkeypatch.setattr(lifecycle, "save_proxy_config", lambda pid, cfg: saved.update({pid: cfg}) or True)
    lifecycle.handle(
        _req("install", service_meta_id="stub@1.0.0", proxy_name="cfg-1", placeholder=True)
    )
    lifecycle.handle(_req("start", service_proxy_id="cfg-1", config={"enable_detection": True}))
    proxy = db.get_item("service_proxy", "cfg-1")
    assert proxy["status"] == "running"
    # Applying config marks the instance configured (config gate won't
    # re-prompt on later starts).
    assert proxy["configured"] is True
    # The merged config persisted to the yml, and NOT to the row.
    assert saved["cfg-1"]["enable_detection"] is True
    assert "service_config" not in proxy


def test_start_with_config_persists_to_yml(db, monkeypatch):
    # Wizard-supplied config at start is written to the yml (the source of
    # truth) before the spawn, so the spawn + a restart both see it.
    saved = []
    monkeypatch.setattr(lifecycle, "proxy_config", lambda pid: {})
    monkeypatch.setattr(lifecycle, "save_proxy_config", lambda pid, cfg: saved.append(pid) or True)
    lifecycle.handle(_req("install", service_meta_id="stub@1.0.0", proxy_name="cfg-as", placeholder=True))
    lifecycle.handle(_req("start", service_proxy_id="cfg-as", config={"enable_detection": True}))
    assert saved == ["cfg-as"]


def test_plain_start_does_not_persist(db, monkeypatch):
    # A reconcile-driven start (no wizard config) must NOT re-write yml —
    # the yml is already authoritative, and writing on every boot-time start
    # would be N redundant writes.
    saved = []
    monkeypatch.setattr(lifecycle, "proxy_config", lambda pid: {})
    monkeypatch.setattr(lifecycle, "save_proxy_config", lambda pid, cfg: saved.append(pid) or True)
    lifecycle.handle(_req("install", service_meta_id="stub@1.0.0", proxy_name="cfg-plain", placeholder=True))
    lifecycle.handle(_req("start", service_proxy_id="cfg-plain"))
    assert saved == []


def test_install_refuses_duplicate_proxy_name(db):
    """Creating a second proxy with the same name must fail and must
    NOT clobber the existing row. This is the user-facing 'no two
    services with the same name, period' guarantee.
    """
    first = lifecycle.handle(
        _req("install", service_meta_id="stub@1.0.0", proxy_name="dup-1")
    )
    assert first["status"] == "completed"
    pid_before = db.get_item("service_proxy", "dup-1").get("pid")

    second = lifecycle.handle(
        _req("install", service_meta_id="stub@1.0.0", proxy_name="dup-1")
    )
    assert second["status"] == "failed"
    assert "already exists" in (second.get("result") or "")

    # Original row untouched — pid still matches the first install's,
    # status still 'installed', service_meta_id still bound.
    row = db.get_item("service_proxy", "dup-1")
    assert row is not None
    assert row["service_meta_id"] == "stub@1.0.0"
    assert row["status"] == "installed"
    assert row.get("pid") == pid_before


def test_concurrent_install_only_one_winner(db):
    """Two install requests for the same name fired from concurrent
    threads must produce exactly ONE row. Reproduces the race that
    used to allow duplicate proxies.
    """
    import threading
    results: list = []
    errors: list = []

    def _do_install() -> None:
        try:
            results.append(lifecycle.handle(
                _req("install", service_meta_id="stub@1.0.0", proxy_name="race-1")
            ))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_do_install) for _ in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()

    completed = [r for r in results if r["status"] == "completed"]
    failed = [r for r in results if r["status"] == "failed"]
    assert len(completed) == 1, (
        f"expected 1 winning install, got {len(completed)}: {completed}"
    )
    assert len(failed) == len(threads) - 1, (
        f"expected {len(threads)-1} 'already exists' failures, got {len(failed)}: {failed}"
    )
    for f in failed:
        assert "already exists" in (f.get("result") or "")
    # Exactly one row in the DB regardless of concurrent fire.
    rows = [p for p in db.get_all_items("service_proxy") if p.get("id") == "race-1"]
    assert len(rows) == 1


def test_full_lifecycle_round_trip(db):
    lifecycle.handle(
        _req("install", service_meta_id="stub@1.0.0", proxy_name="echo-1")
    )
    lifecycle.handle(_req("start", service_proxy_id="echo-1"))
    proxy = db.get_item("service_proxy", "echo-1")
    assert proxy["status"] == "running"
    assert proxy["pid"] is not None
    assert proxy["started_at"]

    lifecycle.handle(_req("stop", service_proxy_id="echo-1"))
    proxy = db.get_item("service_proxy", "echo-1")
    assert proxy["status"] == "stopped"
    assert proxy["pid"] is None
    assert proxy["stopped_at"]

    lifecycle.handle(_req("uninstall", service_proxy_id="echo-1"))
    assert db.get_item("service_proxy", "echo-1") is None


def test_install_rejects_unknown_meta(db):
    out = lifecycle.handle(
        _req("install", service_meta_id="ghost@1", proxy_name="x")
    )
    assert out["status"] == "failed"
    assert "not found" in out["result"]


def test_install_rejects_duplicate_proxy(db):
    lifecycle.handle(_req("install", service_meta_id="stub@1.0.0", proxy_name="dup"))
    out = lifecycle.handle(_req("install", service_meta_id="stub@1.0.0", proxy_name="dup"))
    assert out["status"] == "failed"
    assert "already exists" in out["result"]


def test_start_requires_installed(db):
    out = lifecycle.handle(_req("start", service_proxy_id="missing"))
    assert out["status"] == "failed"


def test_cannot_start_running_proxy(db):
    lifecycle.handle(_req("install", service_meta_id="stub@1.0.0", proxy_name="busy"))
    lifecycle.handle(_req("start", service_proxy_id="busy"))
    out = lifecycle.handle(_req("start", service_proxy_id="busy"))
    assert out["status"] == "failed"
    assert "from status='running'" in out["result"]


def test_restart_from_running(db):
    lifecycle.handle(_req("install", service_meta_id="stub@1.0.0", proxy_name="r"))
    lifecycle.handle(_req("start", service_proxy_id="r"))
    first_pid = db.get_item("service_proxy", "r")["pid"]
    lifecycle.handle(_req("restart", service_proxy_id="r"))
    proxy = db.get_item("service_proxy", "r")
    assert proxy["status"] == "running"
    # New pid after restart (mock pids include a random component).
    assert proxy["pid"] != first_pid or first_pid is None


def test_uninstall_refuses_running(db):
    lifecycle.handle(_req("install", service_meta_id="stub@1.0.0", proxy_name="alive"))
    lifecycle.handle(_req("start", service_proxy_id="alive"))
    out = lifecycle.handle(_req("uninstall", service_proxy_id="alive"))
    assert out["status"] == "failed"
    assert "stop it first" in out["result"]


def test_unknown_action(db):
    out = lifecycle.handle(_req("teleport", service_proxy_id="x"))
    assert out["status"] == "failed"
    assert "unsupported action" in out["result"]


# ─── workspace activation ───────────────────────────────────────────────


def _seed_workspace(db, ws_id="ws-1", proxy_ids=("a", "b")):
    for pid in proxy_ids:
        lifecycle.handle(
            _req("install", service_meta_id="stub@1.0.0", proxy_name=pid)
        )
    db.insert_item(
        "workspace",
        ws_id,
        {"id": ws_id, "name": ws_id, "service_proxy_ids": list(proxy_ids)},
    )
    return ws_id


def test_activate_workspace_starts_all_proxies(db):
    _seed_workspace(db, "ws-1", ("a", "b"))
    out = lifecycle.handle(_req("activate_workspace", workspace_id="ws-1"))
    assert out["status"] == "completed"
    workspace = db.get_item("workspace", "ws-1")
    assert workspace["status"] == "active"
    assert db.get_item("service_proxy", "a")["status"] == "running"
    assert db.get_item("service_proxy", "b")["status"] == "running"


def test_activate_workspace_missing_proxy_marks_degraded(db):
    _seed_workspace(db, "ws-1", ("a",))
    workspace = db.get_item("workspace", "ws-1")
    workspace["service_proxy_ids"] = ["a", "ghost"]
    db.update_item("workspace", "ws-1", workspace)

    out = lifecycle.handle(_req("activate_workspace", workspace_id="ws-1"))
    workspace = db.get_item("workspace", "ws-1")
    assert workspace["status"] == "degraded"
    # The summary string reflects the workspace's terminal status.
    # Per-proxy failure detail rides the /workspace/{id}/activation bus
    # topic (where the UI consumes it), not the request result string.
    assert "degraded" in (out["result"] or "")
    assert db.get_item("service_proxy", "a")["status"] == "running"


def test_deactivate_workspace_stops_all_running(db):
    _seed_workspace(db, "ws-1", ("a", "b"))
    lifecycle.handle(_req("activate_workspace", workspace_id="ws-1"))

    lifecycle.handle(_req("deactivate_workspace", workspace_id="ws-1"))
    workspace = db.get_item("workspace", "ws-1")
    assert workspace["status"] == "inactive"
    assert db.get_item("service_proxy", "a")["status"] == "stopped"
    assert db.get_item("service_proxy", "b")["status"] == "stopped"


def test_deactivate_workspace_tolerates_missing_proxies(db):
    _seed_workspace(db, "ws-1", ("a",))
    workspace = db.get_item("workspace", "ws-1")
    workspace["service_proxy_ids"] = ["a", "ghost"]
    db.update_item("workspace", "ws-1", workspace)

    out = lifecycle.handle(_req("deactivate_workspace", workspace_id="ws-1"))
    # Missing proxies on deactivate are treated as already-stopped, not failure.
    assert out["status"] == "completed"
    workspace = db.get_item("workspace", "ws-1")
    assert workspace["status"] == "inactive"


def test_activate_workspace_no_proxies_succeeds(db):
    db.insert_item("workspace", "empty", {"id": "empty", "name": "empty", "service_proxy_ids": []})
    out = lifecycle.handle(_req("activate_workspace", workspace_id="empty"))
    assert out["status"] == "completed"
    assert db.get_item("workspace", "empty")["status"] == "active"


def test_activate_workspace_unknown_id(db):
    out = lifecycle.handle(_req("activate_workspace", workspace_id="nope"))
    assert out["status"] == "failed"
    assert "not found" in out["result"]


# ─── canonical action verbs + legacy aliases ────────────────────────────


def test_create_service_canonical_verb(db):
    out = lifecycle.handle(
        _req("create_service", service_meta_id="stub@1.0.0", proxy_name="canonical-1")
    )
    assert out["status"] == "completed"
    # handle() rewrites action to the canonical form regardless of input.
    assert out["action"] == "create_service"
    assert db.get_item("service_proxy", "canonical-1")["status"] == "installed"


def test_install_alias_normalizes_to_create_service(db):
    out = lifecycle.handle(
        _req("install", service_meta_id="stub@1.0.0", proxy_name="aliased-1")
    )
    assert out["status"] == "completed"
    # The legacy verb survives as input but the canonical name is what
    # gets persisted on the request row.
    assert out["action"] == "create_service"


def test_release_service_drops_registry_row(db):
    lifecycle.handle(_req("create_service", service_meta_id="stub@1.0.0", proxy_name="r1"))
    out = lifecycle.handle(_req("release_service", service_proxy_id="r1"))
    assert out["status"] == "completed"
    assert out["action"] == "release_service"
    assert db.get_item("service_proxy", "r1") is None


def test_uninstall_alias_normalizes_to_release_service(db):
    lifecycle.handle(_req("create_service", service_meta_id="stub@1.0.0", proxy_name="r2"))
    out = lifecycle.handle(_req("uninstall", service_proxy_id="r2"))
    assert out["status"] == "completed"
    assert out["action"] == "release_service"
    assert db.get_item("service_proxy", "r2") is None


@pytest.fixture
def config_set(monkeypatch, tmp_path):
    """Point the lifecycle's config-set IO at a real tmp_path dir, so
    release_service's registry write actually happens (it's wrapped in a
    swallow-all try/except in production, so a test using the bare ``db``
    fixture alone would silently no-op this part). Pins the active set
    name explicitly via monkeypatch — ``config_sets._BOOTED_SET`` is a
    process-global some OTHER test module sets directly via
    ``pin_booted_set()`` (not through monkeypatch), so without this the
    active set here depends on test run order."""
    from botgear.runtime import config_sets
    data_dir = tmp_path / "data"
    (data_dir / "config_sets" / "test-set").mkdir(parents=True)
    monkeypatch.setattr(lifecycle, "_resolve_data_dir", lambda: data_dir)
    monkeypatch.setattr(lifecycle, "_resolve_repo_dir", lambda: tmp_path)
    monkeypatch.setattr(config_sets, "_BOOTED_SET", "test-set")
    return data_dir


def test_release_preserves_proxy_yml(db, config_set):
    # Config (not a destructive release target) should survive: it took
    # real work to get a service's config "valid" and release must not
    # throw that away just because the operator removed it from the
    # canvas — see docs/README_SERVICE_LIFECYCLE.md §5.
    lifecycle.handle(_req("create_service", service_meta_id="stub@1.0.0", proxy_name="r3"))
    lifecycle.save_proxy_config("r3", {"api_key": "shh", "region": "us-east"})
    yml_path = config_set / "config_sets" / "test-set" / "r3.yml"
    assert yml_path.is_file()

    out = lifecycle.handle(_req("release_service", service_proxy_id="r3"))
    assert out["status"] == "completed"
    assert db.get_item("service_proxy", "r3") is None
    # The row is gone but the yml — and its config — is untouched.
    assert yml_path.is_file()
    assert lifecycle.proxy_config("r3") == {"api_key": "shh", "region": "us-east"}


def test_release_then_recreate_same_name_reuses_preserved_config(db, config_set):
    # This is the swap-by-name workflow: releasing "r4" and later creating
    # a brand NEW instance (possibly a different type entirely) under the
    # same name should transparently pick up whatever yml is sitting at
    # that name — config is keyed by proxy_id, not by DB-row identity.
    lifecycle.handle(_req("create_service", service_meta_id="stub@1.0.0", proxy_name="r4"))
    lifecycle.save_proxy_config("r4", {"voice": "amy"})
    lifecycle.handle(_req("release_service", service_proxy_id="r4"))

    lifecycle.handle(_req("create_service", service_meta_id="stub@1.0.0", proxy_name="r4"))
    assert lifecycle.proxy_config("r4") == {"voice": "amy"}


def test_start_stop_service_canonical_verbs(db):
    lifecycle.handle(_req("create_service", service_meta_id="stub@1.0.0", proxy_name="ss"))

    start_out = lifecycle.handle(_req("start_service", service_proxy_id="ss"))
    assert start_out["status"] == "completed"
    assert start_out["action"] == "start_service"
    assert db.get_item("service_proxy", "ss")["status"] == "running"

    stop_out = lifecycle.handle(_req("stop_service", service_proxy_id="ss"))
    assert stop_out["status"] == "completed"
    assert stop_out["action"] == "stop_service"
    assert db.get_item("service_proxy", "ss")["status"] == "stopped"


def test_unknown_action_still_rejected(db):
    out = lifecycle.handle(_req("teleport_service", service_proxy_id="x"))
    assert out["status"] == "failed"
    assert "unsupported action" in out["result"]


# ─── type install / uninstall (M6) ──────────────────────────────────────

@pytest.fixture
def pip_meta(db, monkeypatch, tmp_path):
    """Seed a pip-shaped service type and neutralize real venv/pip/repo IO.

    fake install creates a <slot>/.venv/bin dir (so the next start
    short-circuits); fake uninstall removes it. Returns a call counter so
    tests can assert a from-scratch reinstall each cycle.
    """
    db.insert_item("service_meta", "pipsvc@1.0.0", {
        "id": "pipsvc@1.0.0", "name": "pipsvc", "version": "1.0.0",
        "dependency_manager": "pip", "package_spec": "-e ${APP_ROOT}/x",
        "installed": False,
    })
    monkeypatch.setattr(lifecycle, "_resolve_repo_dir", lambda: tmp_path)
    calls = {"install": 0, "uninstall": 0}

    def _venv(slot):
        name, ver = slot.split("/", 1)
        return tmp_path / name / ver / ".venv"

    def fake_install(dep, spec, slot, repo_dir, **kw):
        calls["install"] += 1
        (_venv(slot) / "bin").mkdir(parents=True, exist_ok=True)
        return _venv(slot) / "bin"

    def fake_uninstall(slot, repo_dir):
        import shutil
        calls["uninstall"] += 1
        v = _venv(slot)
        if v.exists():
            shutil.rmtree(v)

    monkeypatch.setattr(lifecycle.installer, "install", fake_install)
    monkeypatch.setattr(lifecycle.installer, "uninstall_type", fake_uninstall)
    return calls


def test_install_marks_meta_installed(db, pip_meta):
    # Lazy install-on-Start flips service_meta.installed=True (badge/gate).
    lifecycle.handle(_req("install", service_meta_id="pipsvc@1.0.0", proxy_name="p1", placeholder=True))
    assert db.get_item("service_meta", "pipsvc@1.0.0")["installed"] is False
    lifecycle.handle(_req("start", service_proxy_id="p1"))
    assert db.get_item("service_proxy", "p1")["status"] == "running"
    assert db.get_item("service_meta", "pipsvc@1.0.0")["installed"] is True
    assert pip_meta["install"] == 1


def test_uninstall_type_removes_venv_and_resets_instances(db, pip_meta):
    lifecycle.handle(_req("install", service_meta_id="pipsvc@1.0.0", proxy_name="p1", placeholder=True))
    lifecycle.handle(_req("start", service_proxy_id="p1"))
    lifecycle.handle(_req("stop", service_proxy_id="p1"))
    out = lifecycle.handle(_req("uninstall_type", service_meta_id="pipsvc@1.0.0"))
    assert out["status"] == "completed"
    assert pip_meta["uninstall"] == 1
    assert db.get_item("service_meta", "pipsvc@1.0.0")["installed"] is False
    # surviving instance reverts to a placeholder
    assert db.get_item("service_proxy", "p1")["status"] == "placeholder"


def test_uninstall_type_refused_while_running(db, pip_meta):
    lifecycle.handle(_req("install", service_meta_id="pipsvc@1.0.0", proxy_name="p2", placeholder=True))
    lifecycle.handle(_req("start", service_proxy_id="p2"))
    out = lifecycle.handle(_req("uninstall_type", service_meta_id="pipsvc@1.0.0"))
    assert out["status"] == "failed"
    assert "stop these" in (out["result"] or "")
    assert pip_meta["uninstall"] == 0
    assert db.get_item("service_meta", "pipsvc@1.0.0")["installed"] is True


def test_install_uninstall_cycle_repeatable(db, pip_meta):
    lifecycle.handle(_req("install", service_meta_id="pipsvc@1.0.0", proxy_name="c1", placeholder=True))
    for _ in range(2):
        lifecycle.handle(_req("start", service_proxy_id="c1"))
        assert db.get_item("service_proxy", "c1")["status"] == "running"
        assert db.get_item("service_meta", "pipsvc@1.0.0")["installed"] is True
        lifecycle.handle(_req("stop", service_proxy_id="c1"))
        lifecycle.handle(_req("uninstall_type", service_meta_id="pipsvc@1.0.0"))
        assert db.get_item("service_meta", "pipsvc@1.0.0")["installed"] is False
        assert db.get_item("service_proxy", "c1")["status"] == "placeholder"
    # a fresh from-scratch install + uninstall each cycle
    assert pip_meta["install"] == 2
    assert pip_meta["uninstall"] == 2


def test_start_self_heals_missing_install(db, pip_meta):
    # A stopped (non-placeholder) instance whose type venv vanished
    # reinstalls on Start rather than launching against a missing venv.
    lifecycle.handle(_req("install", service_meta_id="pipsvc@1.0.0", proxy_name="h1", placeholder=True))
    lifecycle.handle(_req("start", service_proxy_id="h1"))
    lifecycle.handle(_req("stop", service_proxy_id="h1"))
    # Simulate the venv disappearing without resetting the instance:
    meta = db.get_item("service_meta", "pipsvc@1.0.0"); meta["installed"] = False
    db.update_item("service_meta", "pipsvc@1.0.0", meta)
    lifecycle.installer.uninstall_type("pipsvc/1.0.0", None)
    before = pip_meta["install"]
    lifecycle.handle(_req("start", service_proxy_id="h1"))
    assert db.get_item("service_proxy", "h1")["status"] == "running"
    assert pip_meta["install"] == before + 1


# ─── rename_service ───────────────────────────────────────────────────


def _set_dir(data_dir):
    return data_dir / "config_sets" / "test-set"


def test_rename_rekeys_row_yml_registry_and_config(db, config_set):
    from botgear.runtime.config_sets import load_runtime_yml

    lifecycle.handle(_req("create_service", service_meta_id="stub@1.0.0", proxy_name="cam"))
    lifecycle.save_proxy_config("cam", {"fps": 30})

    report = lifecycle.rename_service("cam", "cam-front")

    assert db.get_item("service_proxy", "cam") is None
    row = db.get_item("service_proxy", "cam-front")
    assert row["id"] == "cam-front" and row["name"] == "cam-front"
    assert row["service_meta_id"] == "stub@1.0.0"
    assert not (_set_dir(config_set) / "cam.yml").exists()
    assert (_set_dir(config_set) / "cam-front.yml").is_file()
    assert "cam" not in load_runtime_yml(_set_dir(config_set)).registry
    assert "cam-front" in load_runtime_yml(_set_dir(config_set)).registry
    assert lifecycle.proxy_config("cam-front") == {"fps": 30}
    assert report["yml_renamed"] and report["registry_updated"]


def test_rename_preserves_registry_order(db, config_set):
    from botgear.runtime.config_sets import load_runtime_yml, write_runtime_registry

    for name in ("a1", "b1", "c1"):
        lifecycle.handle(_req("create_service", service_meta_id="stub@1.0.0", proxy_name=name))
        lifecycle.save_proxy_config(name, {"x": 1})
    write_runtime_registry(_set_dir(config_set), ["a1", "b1", "c1"])

    lifecycle.rename_service("b1", "bee")

    assert load_runtime_yml(_set_dir(config_set)).registry == ["a1", "bee", "c1"]


def test_rename_rewrites_references_in_other_configs(db, config_set):
    lifecycle.handle(_req("create_service", service_meta_id="stub@1.0.0", proxy_name="arduino-1"))
    lifecycle.handle(_req("create_service", service_meta_id="stub@1.0.0", proxy_name="servo-1"))
    lifecycle.save_proxy_config("arduino-1", {"port": "/dev/ttyACM0"})
    lifecycle.save_proxy_config("servo-1", {
        "controller_id": "arduino-1",
        "name": "arduino-1",                      # self-identity key: never a reference
        "input": {"topic": "/arduino/arduino-1/state"},
        "input_ref": "/arduino/arduino-10/state",  # a different proxy — untouched
        "controllers": ["arduino-1", "other"],
    })

    report = lifecycle.rename_service("arduino-1", "uno")

    cfg = lifecycle.proxy_config("servo-1")
    assert cfg["controller_id"] == "uno"
    assert cfg["name"] == "arduino-1"
    assert cfg["input"]["topic"] == "/arduino/uno/state"
    assert cfg["input_ref"] == "/arduino/arduino-10/state"
    assert cfg["controllers"] == ["uno", "other"]
    assert report["references"] == {"servo-1": 3}


def test_rename_rekeys_workspace_refs(db, config_set):
    lifecycle.handle(_req("create_service", service_meta_id="stub@1.0.0", proxy_name="m1"))
    db.insert_item("workspace", "w", {
        "id": "w", "kind": "user",
        "service_proxy_ids": ["m1", "other"],
        "node_positions": {"m1": {"x": 1, "y": 2}, "other": {"x": 0, "y": 0}},
        "node_view_types": {"m1": "view_full"},
        "edges": [{"id": "e1", "source": "m1", "target": "other"}],
    })
    db.insert_item("workspace", "untouched", {"id": "untouched", "kind": "user", "service_proxy_ids": ["other"]})

    report = lifecycle.rename_service("m1", "motor")

    ws = db.get_item("workspace", "w")
    assert ws["service_proxy_ids"] == ["motor", "other"]
    assert ws["node_positions"] == {"motor": {"x": 1, "y": 2}, "other": {"x": 0, "y": 0}}
    assert ws["node_view_types"] == {"motor": "view_full"}
    assert ws["edges"][0]["source"] == "motor"
    assert report["workspaces"] == ["w"]


def test_rename_moves_brain_workspace_dir(db, config_set):
    lifecycle.handle(_req("create_service", service_meta_id="stub@1.0.0", proxy_name="brain-1"))
    (config_set / "brain" / "brain-1" / "workflows").mkdir(parents=True)

    lifecycle.rename_service("brain-1", "cortex")

    assert not (config_set / "brain" / "brain-1").exists()
    assert (config_set / "brain" / "cortex" / "workflows").is_dir()


def test_rename_refused_while_running(db, config_set):
    lifecycle.handle(_req("create_service", service_meta_id="stub@1.0.0", proxy_name="live"))
    lifecycle.handle(_req("start_service", service_proxy_id="live"))

    with pytest.raises(ValueError, match="stop it first"):
        lifecycle.rename_service("live", "dead")
    assert db.get_item("service_proxy", "live") is not None
    assert db.get_item("service_proxy", "dead") is None


@pytest.mark.parametrize("new", ["Cam", "a/b", "1x", ""])
def test_rename_rejects_invalid_names(db, config_set, new):
    lifecycle.handle(_req("create_service", service_meta_id="stub@1.0.0", proxy_name="ok"))
    with pytest.raises(ValueError, match="invalid service name"):
        lifecycle.rename_service("ok", new)


def test_rename_refuses_taken_name_and_parked_yml(db, config_set):
    lifecycle.handle(_req("create_service", service_meta_id="stub@1.0.0", proxy_name="x1"))
    lifecycle.handle(_req("create_service", service_meta_id="stub@1.0.0", proxy_name="x2"))
    with pytest.raises(ValueError, match="already exists"):
        lifecycle.rename_service("x1", "x2")

    (_set_dir(config_set) / "parked.yml").write_text("type: stub@1.0.0\n")
    with pytest.raises(ValueError, match="parked config"):
        lifecycle.rename_service("x1", "parked")
    assert db.get_item("service_proxy", "x1") is not None


def test_rename_refuses_singleton(db, config_set):
    db.insert_item("service_meta", "runtime@1.0.0",
                   {"id": "runtime@1.0.0", "name": "runtime", "tags": ["singleton"]})
    db.insert_item("service_proxy", "runtime",
                   {"id": "runtime", "name": "runtime", "service_meta_id": "runtime@1.0.0", "status": "running"})
    with pytest.raises(ValueError, match="singleton"):
        lifecycle.rename_service("runtime", "rt")


def test_rename_unknown_raises_not_found(db, config_set):
    with pytest.raises(ValueError, match="not found"):
        lifecycle.rename_service("ghost", "spirit")
