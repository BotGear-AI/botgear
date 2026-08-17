# unmanaged
"""Tests for boot-from-config-set (stone 4)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

SRC = Path(__file__).parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

REPO_DIR = Path(__file__).parent.parent / "repo"

from botgear.runtime.boot import (  # noqa: E402
    MIGRATION_MARKER,
    _effective_registry,
    _registry_members,
    _set_is_empty,
    bootstrap_security_core,
    migrate_db_to_config_set,
    sync_config_set_to_db,
)
from botgear.runtime.config_sets import load_runtime_yml  # noqa: E402
from botgear.runtime.repo import scan_repo  # noqa: E402


@pytest.fixture(scope="module")
def manifests():
    return {m.id: m for m in scan_repo(REPO_DIR.resolve())}


# ─── stub DB (in-memory, mimics TinyDB adapter surface) ───────────────


class StubDB:
    """Minimal in-memory stand-in for the DatabaseAdapter used by boot.py.

    Only the methods boot.py calls are implemented; everything else
    would never be exercised in stone-4 tests."""

    def __init__(self):
        self.tables: Dict[str, Dict[str, Any]] = {"service_proxy": {}}

    def get_all_items(self, table: str) -> List[Dict[str, Any]]:
        return list(self.tables.get(table, {}).values())

    def get_item(self, table: str, key: str) -> Dict[str, Any]:
        # Mirror the REAL adapter (database.tinydb): a missing row returns {},
        # NOT None. Code must test emptiness (`if not row`), never identity
        # (`is None`) — this fidelity is what catches the sync create-vs-update
        # bug where `is None` sent every new registry member down update.
        result = self.tables.get(table, {}).get(key)
        return result if result else {}

    def insert_item(self, table: str, key: str, row: Dict[str, Any]) -> None:
        self.tables.setdefault(table, {})[key] = dict(row)

    def update_item(self, table: str, key: str, updates: Dict[str, Any], *, include_nulls=True) -> Dict[str, Any]:
        # Mirror the real adapter: SKIP (no upsert) when the row is absent, and
        # MERGE fields into the existing row. An update can NEVER create a row —
        # that's precisely what surfaced the sync create-vs-update bug.
        existing = self.tables.setdefault(table, {}).get(key)
        if not existing:
            return {}
        filtered = {k: v for k, v in updates.items() if k != "id"}
        if not include_nulls:
            filtered = {k: v for k, v in filtered.items() if v is not None}
        existing.update(filtered)
        return existing

    def delete_item(self, table: str, key: str) -> None:
        self.tables.setdefault(table, {}).pop(key, None)


# ─── empty-set predicate ──────────────────────────────────────────────


def test_set_is_empty_true_for_missing_dir(tmp_path):
    assert _set_is_empty(tmp_path / "no_such_dir") is True


def test_set_is_empty_true_when_only_runtime_yml(tmp_path):
    (tmp_path / "runtime.yml").write_text("start_order: []")
    assert _set_is_empty(tmp_path) is True


def test_set_is_empty_true_when_only_dotfiles(tmp_path):
    (tmp_path / ".migrated").write_text("x")
    assert _set_is_empty(tmp_path) is True


def test_set_is_empty_false_when_proxy_yml_present(tmp_path):
    (tmp_path / "clock-1.yml").write_text("type: clock@1.0.0")
    assert _set_is_empty(tmp_path) is False


# ─── migrator ─────────────────────────────────────────────────────────


def test_migrate_db_to_config_set_writes_yml_per_row(tmp_path, manifests):
    db = StubDB()
    db.insert_item("service_proxy", "clock-1", {
        "id": "clock-1",
        "service_meta_id": "clock@1.0.0",
        "service_config": {"interval_ms": 500},
        "status": "running",
    })
    db.insert_item("service_proxy", "clock-2", {
        "id": "clock-2",
        "service_meta_id": "clock@1.0.0",
        "service_config": {"interval_ms": 1500},
        "status": "stopped",
    })

    written = migrate_db_to_config_set(db, tmp_path, manifests, REPO_DIR)
    assert sorted(written) == ["clock-1", "clock-2"]
    one = yaml.safe_load((tmp_path / "clock-1.yml").read_text())
    assert one["type"] == "clock@1.0.0"
    assert one["interval_ms"] == 500
    two = yaml.safe_load((tmp_path / "clock-2.yml").read_text())
    assert two["interval_ms"] == 1500
    # Marker file written
    assert (tmp_path / MIGRATION_MARKER).is_file()


def test_migrate_idempotent_skips_existing_files(tmp_path, manifests):
    """Hand-authored yml must NEVER be clobbered by the migrator."""
    db = StubDB()
    db.insert_item("service_proxy", "clock-1", {
        "id": "clock-1",
        "service_meta_id": "clock@1.0.0",
        "service_config": {"interval_ms": 999},
    })
    # Pre-existing hand-authored yml with a different value.
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "clock-1.yml").write_text(yaml.safe_dump({
        "type": "clock@1.0.0",
        "interval_ms": 100,
    }))
    written = migrate_db_to_config_set(db, tmp_path, manifests, REPO_DIR)
    assert written == []  # nothing migrated
    # File content untouched
    out = yaml.safe_load((tmp_path / "clock-1.yml").read_text())
    assert out["interval_ms"] == 100


def test_migrate_skips_unknown_types(tmp_path, manifests):
    """Row pointing at a type not in the registry is logged + skipped,
    not crashed on."""
    db = StubDB()
    db.insert_item("service_proxy", "ghost-1", {
        "id": "ghost-1",
        "service_meta_id": "ghost@99.0.0",
        "service_config": {},
    })
    written = migrate_db_to_config_set(db, tmp_path, manifests, REPO_DIR)
    assert written == []
    assert not (tmp_path / "ghost-1.yml").exists()


# ─── sync (yml → row) ─────────────────────────────────────────────────


def test_sync_creates_row_from_yml(tmp_path, manifests):
    db = StubDB()
    (tmp_path / "clock-1.yml").write_text(yaml.safe_dump({
        "type": "clock@1.0.0", "interval_ms": 750,
    }))
    actions = sync_config_set_to_db(db, tmp_path, manifests, ["clock-1"])
    assert actions == {"clock-1": "created"}
    row = db.get_item("service_proxy", "clock-1")
    assert row                                  # created (missing would be {} — falsy)
    assert row["service_meta_id"] == "clock@1.0.0"
    # stone 4b: config lives in the yml, not the row (ephemera only).
    assert "service_config" not in row
    from botgear.runtime.boot import proxy_config_dict  # noqa: E402
    assert proxy_config_dict(db, REPO_DIR, tmp_path, "clock-1", set_dir=tmp_path)["interval_ms"] == 750


def test_sync_updates_existing_row(tmp_path, manifests):
    db = StubDB()
    db.insert_item("service_proxy", "clock-1", {
        "id": "clock-1",
        "service_meta_id": "clock@1.0.0",
        "service_config": {"interval_ms": 100},
        "status": "running",
        "pid": 12345,
    })
    # Yml on disk says interval_ms=999 — file wins.
    (tmp_path / "clock-1.yml").write_text(yaml.safe_dump({
        "type": "clock@1.0.0", "interval_ms": 999,
    }))
    actions = sync_config_set_to_db(db, tmp_path, manifests, ["clock-1"])
    assert actions == {"clock-1": "updated"}
    row = db.get_item("service_proxy", "clock-1")
    # stone 4b: the legacy blob is stripped — config lives in the yml.
    assert "service_config" not in row
    from botgear.runtime.boot import proxy_config_dict  # noqa: E402
    assert proxy_config_dict(db, REPO_DIR, tmp_path, "clock-1", set_dir=tmp_path)["interval_ms"] == 999
    # Ephemera preserved.
    assert row["status"] == "running"
    assert row["pid"] == 12345


def test_sync_creates_rows_for_all_members_on_fresh_db(tmp_path, manifests):
    """Regression (2026-07-14): on a fresh DB every registry member must get a
    ROW. The adapter's get_item returns {} (not None) for a missing row, so the
    old `if existing is None` check sent new members down the UPDATE branch,
    whose update_item no-ops ("not found") — so 44 of the webxr set's 54
    members never got rows and the UI showed only the ~10 that pre-existed."""
    db = StubDB()  # empty — nothing pre-exists
    members = ["clock-1", "clock-2", "clock-3"]
    for m in members:
        (tmp_path / f"{m}.yml").write_text(yaml.safe_dump(
            {"type": "clock@1.0.0", "interval_ms": 100, "desired_state": "running"}))
    actions = sync_config_set_to_db(db, tmp_path, manifests, members)
    assert actions == {m: "created" for m in members}          # all CREATED, none skipped
    for m in members:
        row = db.get_item("service_proxy", m)
        assert row, f"{m} row was not created"                 # {} (missing) is falsy
        assert row["status"] == "running"
    assert len(db.get_all_items("service_proxy")) == 3


def test_sync_desired_state_running_sets_status(tmp_path, manifests):
    """desired_state is the SOLE status driver: a registry member whose yml
    says running gets status='running'. Membership alone no longer implies it
    (the old start_order fallback is gone)."""
    db = StubDB()
    db.insert_item("service_proxy", "clock-1", {
        "id": "clock-1",
        "service_meta_id": "clock@1.0.0",
        "status": "stopped",
    })
    (tmp_path / "clock-1.yml").write_text(yaml.safe_dump({
        "type": "clock@1.0.0", "interval_ms": 100, "desired_state": "running",
    }))
    sync_config_set_to_db(db, tmp_path, manifests, ["clock-1"])
    assert db.get_item("service_proxy", "clock-1")["status"] == "running"


def test_sync_member_without_desired_state_keeps_status(tmp_path, manifests):
    """A registry member whose yml has no desired_state leaves the existing
    row status as-is — being in the registry does NOT bump it to running."""
    db = StubDB()
    db.insert_item("service_proxy", "clock-1", {
        "id": "clock-1",
        "service_meta_id": "clock@1.0.0",
        "status": "stopped",
    })
    (tmp_path / "clock-1.yml").write_text(yaml.safe_dump({
        "type": "clock@1.0.0", "interval_ms": 100,
    }))
    sync_config_set_to_db(db, tmp_path, manifests, ["clock-1"])
    assert db.get_item("service_proxy", "clock-1")["status"] == "stopped"


def test_sync_prunes_row_not_in_registry(tmp_path, manifests):
    """The registry is authoritative for membership: a non-singleton row NOT
    in the registry is pruned (row deleted) — but its yml is left on disk so
    it can be re-added (parked, not destroyed)."""
    db = StubDB()
    db.insert_item("service_proxy", "clock-1", {
        "id": "clock-1",
        "service_meta_id": "clock@1.0.0",
        "status": "running",
    })
    yml = tmp_path / "clock-1.yml"
    yml.write_text(yaml.safe_dump({"type": "clock@1.0.0", "interval_ms": 200}))
    actions = sync_config_set_to_db(db, tmp_path, manifests, [])   # not in registry
    assert actions == {"clock-1": "pruned:not_in_registry"}
    assert not db.get_item("service_proxy", "clock-1")          # row pruned
    assert yml.exists()                                             # yml kept (parked)


def test_sync_skips_malformed_yml(tmp_path, manifests):
    db = StubDB()
    (tmp_path / "broken.yml").write_text("not: : valid")
    actions = sync_config_set_to_db(db, tmp_path, manifests, ["broken"])
    assert actions["broken"].startswith("skipped:")
    assert not db.get_item("service_proxy", "broken")


def test_sync_missing_yml_surfaces_error_and_continues(tmp_path, manifests):
    """A registry name with no yml is an error but MUST NOT abort the rest."""
    db = StubDB()
    (tmp_path / "clock-1.yml").write_text(yaml.safe_dump({"type": "clock@1.0.0"}))
    actions = sync_config_set_to_db(db, tmp_path, manifests, ["ghost-9", "clock-1"])
    assert actions["ghost-9"] == "error:missing_yml"
    assert actions["clock-1"] == "created"                          # continued past the error
    assert not db.get_item("service_proxy", "ghost-9")          # no phantom row


def test_sync_skips_unknown_type(tmp_path, manifests):
    db = StubDB()
    (tmp_path / "x.yml").write_text(yaml.safe_dump({
        "type": "nonsense@99.0.0",
    }))
    actions = sync_config_set_to_db(db, tmp_path, manifests, ["x"])
    assert actions["x"].startswith("skipped:unknown_type:")


def test_sync_resolves_bare_type_name(tmp_path, manifests):
    """type: clock (no @version) resolves to the unique installed
    version, mirroring the load path."""
    db = StubDB()
    (tmp_path / "clock-1.yml").write_text(yaml.safe_dump({
        "type": "clock",
        "interval_ms": 250,
    }))
    sync_config_set_to_db(db, tmp_path, manifests, ["clock-1"])
    row = db.get_item("service_proxy", "clock-1")
    assert row["service_meta_id"] == "clock@1.0.0"


def test_sync_skips_runtime_singleton_name(tmp_path, manifests):
    """A 'runtime' entry in the registry is the singleton settings file, not
    a per-proxy yml — skipped, never a row."""
    db = StubDB()
    actions = sync_config_set_to_db(db, tmp_path, manifests, ["runtime"])
    assert actions == {"runtime": "skipped:singleton_runtime"}
    assert not db.get_item("service_proxy", "runtime")


# ─── registry membership invariants ───────────────────────────────────


def test_registry_members_equals_glob(tmp_path):
    """The backfill membership MUST equal the historical sorted-glob exactly
    (runtime.yml + dotfiles excluded, security.yml included) so migrating a
    set to an explicit registry boots the identical services in order."""
    (tmp_path / "runtime.yml").write_text("registry: []")
    (tmp_path / ".migrated").write_text("x")
    for name in ("servo-1", "arduino-1", "clock-1", "security"):
        (tmp_path / f"{name}.yml").write_text("type: clock@1.0.0")
    glob = sorted(
        p.stem for p in tmp_path.iterdir()
        if p.is_file() and p.suffix == ".yml"
        and p.name != "runtime.yml" and not p.name.startswith(".")
    )
    assert _registry_members(tmp_path) == glob
    assert _registry_members(tmp_path) == ["arduino-1", "clock-1", "security", "servo-1"]


def test_park_then_readd_round_trip(tmp_path, manifests):
    """The operator's core workflow: drop a name from runtime.registry to PARK
    a service (row gone, yml + config kept), diagnose the simpler system, then
    add the name back to bring it — and its exact config — back."""
    db = StubDB()
    (tmp_path / "clock-a.yml").write_text(yaml.safe_dump(
        {"type": "clock@1.0.0", "interval_ms": 111}))
    b_yml = tmp_path / "clock-b.yml"
    b_yml.write_text(yaml.safe_dump({"type": "clock@1.0.0", "interval_ms": 222}))

    # Full system: both members present.
    sync_config_set_to_db(db, tmp_path, manifests, ["clock-a", "clock-b"])
    assert db.get_item("service_proxy", "clock-a")
    assert db.get_item("service_proxy", "clock-b")

    # Park clock-b by removing it from the registry (yml stays on disk).
    actions = sync_config_set_to_db(db, tmp_path, manifests, ["clock-a"])
    assert actions["clock-b"] == "pruned:not_in_registry"
    assert not db.get_item("service_proxy", "clock-b")      # row parked
    assert b_yml.exists()                                       # yml preserved

    # Add it back: the row returns, rebuilt from the preserved yml (222).
    sync_config_set_to_db(db, tmp_path, manifests, ["clock-a", "clock-b"])
    assert db.get_item("service_proxy", "clock-b")
    from botgear.runtime.boot import proxy_config_dict  # noqa: E402
    assert proxy_config_dict(
        db, REPO_DIR, tmp_path, "clock-b", set_dir=tmp_path)["interval_ms"] == 222


# ─── e2e: migrate then sync round-trips ───────────────────────────────


def test_migrate_then_sync_roundtrips_config(tmp_path, manifests):
    db = StubDB()
    db.insert_item("service_proxy", "clock-1", {
        "id": "clock-1",
        "service_meta_id": "clock@1.0.0",
        "service_config": {"interval_ms": 1234},
        "status": "running",
    })
    migrate_db_to_config_set(db, tmp_path, manifests, REPO_DIR)
    # Wipe the row to simulate a clean DB on subsequent boot.
    db.tables["service_proxy"].clear()
    # Boot derives the registry the same way boot_from_config_set does: an
    # un-migrated set backfills from its present ymls (glob-equivalent).
    registry = _effective_registry(tmp_path, load_runtime_yml(tmp_path))
    actions = sync_config_set_to_db(db, tmp_path, manifests, registry)
    assert actions == {"clock-1": "created"}
    row = db.get_item("service_proxy", "clock-1")
    # stone 4b: config round-trips through the yml (not the row). Pydantic
    # model_dump includes topic_remap (inherited from ServiceConfig); we just
    # check the original value survived.
    assert "service_config" not in row
    from botgear.runtime.boot import proxy_config_dict  # noqa: E402
    assert proxy_config_dict(db, REPO_DIR, tmp_path, "clock-1", set_dir=tmp_path)["interval_ms"] == 1234


# ─── security bootstrap ───────────────────────────────────────────────


def test_bootstrap_security_core_returns_core(tmp_path):
    """A SecurityCore can be instantiated inline during boot — no need
    for the SecurityService to be running yet."""
    core = bootstrap_security_core(tmp_path, repo_dir=REPO_DIR)
    assert core is not None
    ct = core.encrypt("test-secret")
    assert ct.startswith("Encrypted--")
    assert core.decrypt(ct) == "test-secret"
    # Key file landed under tmp_path/security/key.bin
    assert (tmp_path / "security" / "key.bin").is_file()


def test_migrator_encrypts_secrets_when_core_available(tmp_path, manifests):
    """Stone-4 + stone-1 interlock: when a service's config has
    SecretStr fields, the migrator runs them through the encrypt walk
    before they hit disk."""
    # This needs a service type that has a SecretStr field; brain has
    # api_key but it's plain str currently. Use the test EchoService
    # from test_service_config — but it's defined inline there.
    # Instead, just verify the migrator's encrypt_fn parameter wires
    # through to the save path by writing a row whose service_config
    # already carries an Encrypt-- marker.
    db = StubDB()
    db.insert_item("service_proxy", "clock-1", {
        "id": "clock-1",
        "service_meta_id": "clock@1.0.0",
        "service_config": {
            "interval_ms": 500,
            # extra=allow on ServiceConfig means this extra field rides
            # through. The migrator should encrypt it on the way out.
            "tagged_secret": "Encrypt--my-fake-token",
        },
    })
    core = bootstrap_security_core(tmp_path, repo_dir=REPO_DIR)
    written = migrate_db_to_config_set(
        db, tmp_path / "set", manifests, REPO_DIR,
        encrypt_fn=core.encrypt,
    )
    assert written == ["clock-1"]
    out = yaml.safe_load((tmp_path / "set" / "clock-1.yml").read_text())
    # Encrypt-- marker got encrypted, plaintext never reached disk.
    assert out["tagged_secret"].startswith("Encrypted--")
    assert "my-fake-token" not in yaml.safe_dump(out)
    # Round-trip: decrypt the stored value
    assert core.decrypt(out["tagged_secret"]) == "my-fake-token"
