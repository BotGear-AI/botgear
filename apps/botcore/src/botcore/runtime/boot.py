# unmanaged
"""Boot-from-config-set — stone 4 of the TODO_CONFIG_SETS spec.

Glue between the file-based loader (``runtime/config_sets.py``) and the
existing lifecycle that walks ``service_proxy`` rows. Three responsibilities:

  1. **One-shot migrator.** On first boot under the new system — empty
     active set on disk but populated ``service_proxy`` rows — write
     ``<proxy_id>.yml`` for each row, run security through the encrypt
     walk so secrets land as ``Encrypted--`` rather than plaintext.

  2. **Sync.** On every boot, read every yml in the active set, decrypt
     via the security core, validate against ``config_class``, and
     update the matching ``service_proxy`` row. The DB row becomes a
     mirror; the file is authoritative.

  3. **Bootstrap security.** Before any decryption can happen the
     SecurityCore needs to be live. The security *Service* is spawned
     by the normal lifecycle later — but during the sync step we
     instantiate a SecurityCore inline using the same key file, so the
     decrypt callable exists. Same key file → independent SecurityCore
     instances produce identical results.

Stone 4 deliberately does NOT remove the dual-write in
``Service.save_config`` (stone 3 added ``_save_config_to_db_legacy``).
That dual-write is what keeps the DB row in sync with the file between
boots, so the legacy ``reconcile_running_proxies`` path can spawn
services without needing further refactor. Stone 4b (future) strips
the DB write entirely once the loader becomes the only source of
config at boot.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml
from pydantic import SecretStr

from botcore.runtime.config_sets import (
    ConfigSetError,
    active_set_dir,
    active_set_name,
    decrypt_tree,
    discover_candidates,
    encrypt_tree,
    load_runtime_yml,
    save_proxy_yml,
    write_runtime_registry,
)
from botcore.runtime.repo import PackageManifest, repo_roots, root_of, scan_repo, scan_repos


def _all_roots(repo_dir: Path) -> List[Path]:
    """The passed writable ``repo_dir`` plus any read-only
    ``config.repo_paths`` roots, deduped, writable first. Falls back to
    just ``[repo_dir]`` if settings are unavailable (keeps unit tests
    that pass a bare temp repo_dir behaving exactly as single-root)."""
    try:
        from config import get_settings
        roots = repo_roots(get_settings())
    except Exception:  # noqa: BLE001
        return [repo_dir]
    # Ensure the explicitly-passed root is honoured + first.
    out: List[Path] = [repo_dir]
    for r in roots:
        if r != repo_dir and r not in out:
            out.append(r)
    return out


logger = logging.getLogger(__name__)


# Filename prefix for the "this set has been migrated" marker. Kept as
# a hidden file so it doesn't show up in `ls` output the operator
# scrolls past.
MIGRATION_MARKER = ".migrated"


# ─── security bootstrap ───────────────────────────────────────────────


def bootstrap_security_core(data_dir: Path, repo_dir: Optional[Path] = None):
    """Instantiate a SecurityCore directly (NOT via the Service wrapper).

    Returns the core or None if the security module can't be imported
    (e.g. tests that don't ship the repo). The same key file is shared
    with the eventually-spawned SecurityService — independent cores
    over the same key file produce identical results.

    ``repo_dir`` defaults to ``<data_dir>/../repo`` to match the standard
    layout; tests can pass an explicit path.
    """
    import sys
    if repo_dir is None:
        repo_dir = data_dir.parent / "repo"
    candidate = repo_dir / "security" / "1.0.0"
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))
    try:
        import importlib
        sec_mod = importlib.import_module("security")
    except ImportError as exc:
        logger.warning(
            "boot: SecurityCore unavailable (%s) — encrypted configs "
            "will fail to load. Install the security package.", exc,
        )
        return None
    key_path = (data_dir / "security" / "key.bin").resolve()
    return sec_mod.SecurityCore(key_path=key_path)


# ─── migrator ─────────────────────────────────────────────────────────


def _set_is_empty(set_dir: Path) -> bool:
    """An empty set is one with no proxy yml files (runtime.yml + dot
    files don't count). Migration triggers only on truly empty sets so
    a user who's deleted their files doesn't get them silently restored."""
    if not set_dir.is_dir():
        return True
    for p in set_dir.iterdir():
        if not p.is_file():
            continue
        if p.name.startswith("."):
            continue
        if p.suffix != ".yml":
            continue
        if p.name == "runtime.yml":
            continue
        return False
    return True


def _registry_members(set_dir: Path) -> List[str]:
    """File-based membership == the historical glob EXACTLY: the stem of every
    proxy yml in ``sorted(iterdir)`` order (sorted by full filename so it's
    byte-identical to the old glob), excluding runtime.yml + dotfiles,
    INCLUDING security.yml. Used to backfill an un-migrated set's registry."""
    if not set_dir.is_dir():
        return []
    return [
        p.stem for p in sorted(set_dir.iterdir())
        if p.is_file() and p.suffix == ".yml"
        and p.name != "runtime.yml" and not p.name.startswith(".")
    ]


def _effective_registry(set_dir: Path, runtime_yml) -> List[str]:
    """The ordered proxy_ids to create + start. An explicit ``registry`` wins
    (removals stick, order honored, already deduped by the loader). An
    empty/absent registry backfills from the present ymls (glob-equivalent)
    AND persists it, so the set is registry-driven from the next boot on."""
    if runtime_yml.registry:
        return list(runtime_yml.registry)
    stems = _registry_members(set_dir)
    if stems:
        try:
            write_runtime_registry(set_dir, stems)
            logger.info("boot.migrate: backfilled registry set=%s (%d)",
                        set_dir.name, len(stems))
        except Exception:  # noqa: BLE001 — return the in-memory list even if the write fails
            logger.exception("boot.migrate: registry persist failed for %s", set_dir)
    return stems


def _row_to_config_instance(row: Dict[str, Any], manifest: PackageManifest, repo_dir: Path):
    """Reconstruct a Pydantic config_class instance from a service_proxy
    row's ``service_config`` dict. Used by the migrator to round-trip
    through ``save_proxy_yml`` so the encrypted-secrets path runs."""
    from botcore.framework.adapters.in_process import _load_service_class
    module_name = manifest.entry.module or manifest.name
    class_name = manifest.entry.class_name or (
        "".join(p.capitalize() for p in manifest.name.split("_")) + "Service"
    )
    # Load the class from the manifest's OWN root, not the passed
    # writable repo_dir — a type may live in a read-only repo_paths root.
    cls = _load_service_class(
        root_of(manifest), manifest.name, manifest.version, module_name, class_name,
    )
    config_class = getattr(cls, "config_class", None)
    if config_class is None:
        raise RuntimeError(f"{manifest.name} has no config_class")
    # service_config is a plain dict — go straight through Pydantic.
    return config_class(**(row.get("service_config") or {}))


def _write_raw_yml(
    set_dir: Path,
    proxy_id: str,
    type_id: str,
    raw_config: Dict[str, Any],
    encrypt_fn: Optional[Callable[[str], str]] = None,
    desired_state: Optional[str] = None,
) -> None:
    """Fallback write path used when the config_class can't be loaded
    (subprocess services, the runtime singleton, types removed from
    the registry). Walks the raw dict through the encrypt tree before
    writing — secret hygiene is preserved even without Pydantic
    validation.

    ``desired_state`` (when given) is written as a top-level field right
    after ``type:`` — same contract as ``save_proxy_yml``."""
    payload_body = encrypt_tree(raw_config, encrypt_fn)
    payload = {"type": type_id}
    if desired_state is not None:
        payload["desired_state"] = desired_state
    payload.update(payload_body)
    set_dir.mkdir(parents=True, exist_ok=True)
    path = set_dir / f"{proxy_id}.yml"
    tmp = path.with_suffix(".yml.tmp")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False, default_flow_style=False))
    tmp.rename(path)


def _set_desired_state_in_yml(set_dir: Path, proxy_id: str, desired: str) -> bool:
    """Rewrite ONLY the ``desired_state`` of an existing proxy yml, preserving
    every other key (incl. the on-disk encrypted config) byte-for-byte in
    value. Used by save_all for a non-live proxy: its config is already the
    truth on disk, we just record the latest run-intent. Returns False if the
    yml doesn't exist (caller falls back)."""
    path = set_dir / f"{proxy_id}.yml"
    if not path.is_file():
        return False
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return False
    if not isinstance(raw, dict):
        return False
    type_id = raw.pop("type", None)
    raw.pop("desired_state", None)
    payload: Dict[str, Any] = {"type": type_id, "desired_state": desired}
    payload.update(raw)
    tmp = path.with_suffix(".yml.tmp")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False, default_flow_style=False))
    tmp.rename(path)
    return True


def migrate_db_to_config_set(
    db,
    set_dir: Path,
    manifests: Dict[str, PackageManifest],
    repo_dir: Path,
    encrypt_fn: Optional[Callable[[str], str]] = None,
) -> List[str]:
    """Walk every ``service_proxy`` row that has a ``service_config``
    blob; write a yml file for it in ``set_dir``.

    Idempotent — skips rows whose yml file already exists. Returns the
    list of proxy_ids whose files were written (or empty if nothing
    needed migrating).

    Run conditions: set is empty (no proxy ymls present). Caller
    decides whether to invoke; this function does NOT gate on emptiness
    so tests can exercise it on populated sets too.
    """
    rows = db.get_all_items("service_proxy") or []
    written: List[str] = []
    set_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        proxy_id = row.get("id")
        type_id = row.get("service_meta_id")
        if not proxy_id or not type_id:
            continue
        target = set_dir / f"{proxy_id}.yml"
        if target.is_file():
            continue  # operator may have hand-authored; never clobber
        manifest = manifests.get(type_id)
        if manifest is None:
            # Type isn't in the registry — likely a removed package.
            # Skip the row; the operator will see a candidate without
            # type resolution in the UI.
            logger.warning(
                "boot.migrate: skipping %s — service_meta_id=%s not in registry",
                proxy_id, type_id,
            )
            continue
        try:
            config_instance = _row_to_config_instance(row, manifest, repo_dir)
            try:
                save_proxy_yml(
                    set_dir, proxy_id, type_id, config_instance,
                    encrypt_fn=encrypt_fn,
                )
            except Exception:  # noqa: BLE001
                logger.exception("boot.migrate: failed to write %s.yml", proxy_id)
                continue
        except Exception as exc:  # noqa: BLE001
            # Subprocess services + the runtime singleton don't have
            # an in-process Service class to load. Fall back to writing
            # the raw service_config dict — the encryption walk still
            # applies, but Pydantic validation is skipped (the operator
            # gets validation on next load).
            logger.info(
                "boot.migrate: %s has no loadable config_class (%s); "
                "writing raw dict",
                proxy_id, exc,
            )
            try:
                _write_raw_yml(
                    set_dir, proxy_id, type_id,
                    row.get("service_config") or {},
                    encrypt_fn=encrypt_fn,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "boot.migrate: fallback write also failed for %s.yml",
                    proxy_id,
                )
                continue
        written.append(proxy_id)

    # Drop the marker so we can spot in logs that the migrator ran.
    if written:
        (set_dir / MIGRATION_MARKER).write_text(
            f"migrated {len(written)} rows at "
            f"{datetime.now(timezone.utc).isoformat()}\n"
            + "\n".join(written)
        )
    return written


# ─── sync (yml → row) ─────────────────────────────────────────────────


def _safe_load_proxy_yml(path: Path, decrypt_fn) -> Optional[Dict[str, Any]]:
    """Read + decrypt a proxy yml, return raw dict ready to write into
    ``service_config``. Returns None on any parse/decrypt failure
    (logged); caller skips that row."""
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        logger.warning("boot.sync: %s failed to parse: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("boot.sync: %s isn't a yaml mapping", path)
        return None
    type_id = raw.pop("type", None)
    if not isinstance(type_id, str):
        logger.warning("boot.sync: %s missing 'type:' field", path)
        return None
    # desired_state is runtime metadata, not service config — pull it out
    # before the rest of the mapping becomes service_config.
    desired_state = raw.pop("desired_state", None)
    if not isinstance(desired_state, str):
        desired_state = None
    try:
        decrypted = decrypt_tree(raw, decrypt_fn)
    except ConfigSetError as exc:
        logger.warning("boot.sync: %s decrypt failed: %s", path, exc)
        return None
    return {
        "type_id": type_id,
        "service_config": decrypted,
        "desired_state": desired_state,
    }


def _unmask_secrets(value: Any) -> Any:
    """Recursively dump a Pydantic config (or any nested value) to a plain
    dict with SecretStr fields as PLAINTEXT — the shape the spawn's
    ``config_state`` broadcast + the old ``row['service_config']`` carried
    (sync decrypts before storing). Mirrors config_sets._config_to_dict but
    yields raw plaintext rather than ``Encrypt--`` markers."""
    from pydantic import BaseModel, SecretStr
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, BaseModel):
        out: Dict[str, Any] = {}
        for name in type(value).model_fields:
            out[name] = _unmask_secrets(getattr(value, name))
        for name, v in (getattr(value, "__pydantic_extra__", None) or {}).items():
            out.setdefault(name, _unmask_secrets(v))
        return out
    if isinstance(value, dict):
        return {k: _unmask_secrets(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_unmask_secrets(v) for v in value]
    return value


def proxy_config_dict(
    db,
    repo_dir: Path,
    data_dir: Path,
    proxy_id: str,
    *,
    decrypt_fn: Optional[Callable[[str], str]] = None,
    set_dir: Optional[Path] = None,
    allow_row_fallback: bool = True,
) -> Dict[str, Any]:
    """A proxy's CURRENT config as a decrypted plain dict — the single read
    path that replaces ``proxy['service_config']``. Resolution order:

      1. a LIVE in-process Service's ``self.config`` (authoritative while
         running; unmasked so secrets ride through)
      2. else the proxy's yml in the active set (decrypted)
      3. else the DB row's ``service_config`` — TRANSITION fallback, gated by
         ``allow_row_fallback`` so it can be switched off once the DB no
         longer stores config (stone 4b)
      4. else ``{}``

    ``decrypt_fn`` / ``set_dir`` may be passed in to avoid re-bootstrapping
    security + re-resolving the set per call (the link projection loads many
    proxies in one pass)."""
    # 1. live in-process service
    try:
        from botcore import framework
        handle = framework.REGISTRY.get(proxy_id)
        svc = handle.payload.get("service") if handle is not None else None
        if svc is not None and getattr(svc, "config", None) is not None:
            return _unmask_secrets(svc.config)
    except Exception:  # noqa: BLE001 — never let a read break the caller
        logger.debug("proxy_config_dict: live read failed for %s", proxy_id, exc_info=True)
    # 2. yml on disk
    if set_dir is None:
        set_dir = active_set_dir(data_dir)
    path = set_dir / f"{proxy_id}.yml"
    if path.is_file():
        if decrypt_fn is None:
            sec = bootstrap_security_core(data_dir, repo_dir=repo_dir)
            decrypt_fn = sec.decrypt if sec else None
        parsed = _safe_load_proxy_yml(path, decrypt_fn)
        if parsed is not None:
            return dict(parsed.get("service_config") or {})
    # 3. row fallback (transition only)
    if allow_row_fallback:
        row = db.get_item("service_proxy", proxy_id)
        if row and isinstance(row.get("service_config"), dict):
            return dict(row["service_config"])
    return {}


def write_proxy_config(
    db,
    repo_dir: Path,
    data_dir: Path,
    proxy_id: str,
    config_dict: Dict[str, Any],
    *,
    manifests: Optional[Dict[str, PackageManifest]] = None,
    set_dir: Optional[Path] = None,
    encrypt_fn: Optional[Callable[[str], str]] = None,
) -> bool:
    """Persist an EXPLICIT config dict to the proxy's yml — the write path that
    replaces writing ``service_config`` to the DB row. Validates via the type's
    ``config_class`` when it loads (so secrets round-trip + re-encrypt), raw-
    writes otherwise. Skips singletons + unknown proxies. Returns True on write.

    ``desired_state`` is derived from the row's live status, matching
    ``_save_one_proxy``. Never raises — a write hiccup must not break the
    mutation that triggered it."""
    row = db.get_item("service_proxy", proxy_id)
    if not row:
        return False
    type_id = row.get("service_meta_id")
    if not type_id:
        return False
    meta = db.get_item("service_meta", type_id) or {}
    if "singleton" in (meta.get("tags") or []):
        return False
    if manifests is None:
        manifests = {m.id: m for m in scan_repos(_all_roots(repo_dir))}
    if set_dir is None:
        set_dir = active_set_dir(data_dir)
    set_dir.mkdir(parents=True, exist_ok=True)
    if encrypt_fn is None:
        sec = bootstrap_security_core(data_dir, repo_dir=repo_dir)
        encrypt_fn = sec.encrypt if sec else None
    status = row.get("status")
    desired = "running" if status in ("running", "starting", "installing") else "stopped"
    manifest = manifests.get(type_id)
    try:
        inst = None
        if manifest is not None:
            try:
                inst = _row_to_config_instance({"service_config": config_dict}, manifest, repo_dir)
            except Exception:  # noqa: BLE001
                # SUBPROCESS service: its config_class lives in the subprocess's
                # own module + venv and can't be imported in this backend process.
                # Fall back to a raw dict write (secrets still re-encrypted by
                # _write_raw_yml) rather than dropping the config — otherwise a
                # subprocess's live config changes (e.g. webxr teleop mappings)
                # never persist and are silently lost on the next restart.
                logger.debug("write_proxy_config: config_class not loadable for %s; raw-writing", proxy_id)
                inst = None
        if inst is not None:
            save_proxy_yml(set_dir, proxy_id, type_id, inst, encrypt_fn=encrypt_fn, desired_state=desired)
        else:
            _write_raw_yml(set_dir, proxy_id, type_id, config_dict, encrypt_fn=encrypt_fn, desired_state=desired)
        # Append the proxy to the registry on first write so a service created
        # then hard-crashed (before any clean save_all) is still a member on the
        # next boot — matching the old "the yml exists ⇒ it's in the set". This
        # is the single chokepoint for install / wizard / link-bind / patch;
        # deduped + a no-op for an already-listed proxy.
        try:
            reg = load_runtime_yml(set_dir).registry
            if proxy_id not in reg:
                write_runtime_registry(set_dir, reg + [proxy_id])
        except Exception:  # noqa: BLE001
            logger.debug("write_proxy_config: registry append failed for %s", proxy_id, exc_info=True)
        return True
    except Exception:  # noqa: BLE001 — best-effort persist
        logger.exception("boot.write_proxy_config: failed for %s", proxy_id)
        return False


def sync_config_set_to_db(
    db,
    set_dir: Path,
    manifests: Dict[str, PackageManifest],
    registry: List[str],
    decrypt_fn: Optional[Callable[[str], str]] = None,
) -> Dict[str, str]:
    """Create/update a ``service_proxy`` row for every proxy_id in
    ``registry`` (in order), reading its ``<proxy_id>.yml``.

    The REGISTRY is authoritative for MEMBERSHIP: a non-singleton row whose
    id is NOT in the registry is PRUNED — its row is deleted but its
    ``<proxy_id>.yml`` is left untouched, so it drops to a re-addable
    candidate. That is what makes removing a name from ``runtime.registry``
    actually stop that service (config preserved), and it clears rows left
    over from a previously-active set.

    The yml stays authoritative for config + run-state:
      * ``running`` → row status ``running`` (reconcile spawns it),
      * ``stopped`` → row status ``stopped`` (visible/idle),
      * neither → ``installed`` (created but not started — ``desired_state``
        is the SOLE status driver; a member with no ``desired_state`` is not
        spawned).
    A registry name whose ``<proxy_id>.yml`` is MISSING is surfaced as
    ``error:missing_yml`` and skipped WITHOUT aborting the rest.

    Returns proxy_id → action ('created' | 'updated' | 'skipped:<reason>' |
    'error:missing_yml' | 'pruned:not_in_registry').
    """
    actions: Dict[str, str] = {}
    if not set_dir.is_dir():
        return actions
    for proxy_id in registry:
        if proxy_id == "runtime":
            # runtime.yml is the singleton settings file, not a per-proxy yml.
            actions[proxy_id] = "skipped:singleton_runtime"
            continue
        path = set_dir / f"{proxy_id}.yml"
        if not path.is_file():
            # Visible error, but DON'T abort — the other services still start.
            actions[proxy_id] = "error:missing_yml"
            continue
        parsed = _safe_load_proxy_yml(path, decrypt_fn)
        if parsed is None:
            actions[proxy_id] = "skipped:parse_or_decrypt_failed"
            continue
        type_id = parsed["type_id"]
        # Resolve bare names (clock → clock@1.0.0) the same way the
        # loader does, so the migrator-produced yml and hand-authored
        # yml behave identically.
        if type_id not in manifests:
            matches = [m for m in manifests.values() if m.name == type_id]
            if len(matches) == 1:
                type_id = matches[0].id
            elif len(matches) > 1:
                actions[proxy_id] = f"skipped:ambiguous_type:{type_id}"
                continue
            else:
                actions[proxy_id] = f"skipped:unknown_type:{type_id}"
                continue

        desired_state = parsed.get("desired_state")

        # NB: the DB adapter's get_item returns {} (NOT None) for a missing
        # row, so test emptiness — not identity. `is None` here silently sent
        # every never-before-seen registry member down the update branch, whose
        # update_item then no-ops ("not found") → the row was never created.
        existing = db.get_item("service_proxy", proxy_id)
        if not existing:
            new_row = {
                "id": proxy_id,
                "name": proxy_id,
                "service_meta_id": type_id,
                # stone 4b: config lives in the yml, NOT the row. Reads go
                # through proxy_config_dict (yml/live), so the row is ephemera.
                "status": "installed",
                "configured": False,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "installed_at": datetime.now(timezone.utc).isoformat(),
                "pid": None,
                "host": None,
                "port": None,
                "started_at": None,
                "stopped_at": None,
                "error": None,
            }
            if desired_state == "running":
                # reconcile_running_proxies looks for status=running/starting.
                new_row["status"] = "running"
            elif desired_state == "stopped":
                # Instantiate/visible but idle — reconcile leaves it alone.
                new_row["status"] = "stopped"
            # else: no desired_state → row stays "installed" (created, not
            # spawned). desired_state is the sole status driver — membership
            # (the registry) no longer implies running.
            db.insert_item("service_proxy", proxy_id, new_row)
            actions[proxy_id] = "created"
            continue

        # Update existing row. If the operator changed the type via file
        # rename (swap-by-rename), the new type_id wins.
        existing["service_meta_id"] = type_id
        # stone 4b: config lives in the yml; strip any legacy blob off the row
        # (one-shot migration — old persisted rows shed it on first sync).
        had_legacy_config = "service_config" in existing
        existing.pop("service_config", None)
        # desired_state is AUTHORITATIVE over the persisted status: the row
        # may be stale-'running' from a process that died uncleanly. Reset
        # pid/error so reconcile starts from a clean slate.
        if desired_state == "running":
            existing["status"] = "running"
            existing["error"] = None
            existing["pid"] = None
        elif desired_state == "stopped":
            existing["status"] = "stopped"
            existing["error"] = None
            existing["pid"] = None
        # else: no desired_state → leave the persisted status as-is (the yml
        # doesn't dictate a run-state); membership no longer implies running.
        if had_legacy_config:
            # The DB adapter's update_item MERGES (it can't delete a key), so
            # replace the row to physically shed the legacy config blob.
            db.delete_item("service_proxy", proxy_id)
            db.insert_item("service_proxy", proxy_id, existing)
        else:
            db.update_item("service_proxy", proxy_id, existing, include_nulls=True)
        actions[proxy_id] = "updated"

    # ── PRUNE: registry is authoritative for the active set ──────────────
    # Any non-singleton row whose id is NOT in the registry is PARKED: delete
    # its row (its <proxy_id>.yml is left on disk → it becomes a re-addable
    # candidate). This makes "remove a name from runtime.registry" actually
    # stop that service, and clears rows from a previously-active set.
    # Singletons (runtime/security) ride the boot — never pruned.
    reg_set = set(registry)
    for row in list(db.get_all_items("service_proxy") or []):
        rid = row.get("id")
        if not rid or rid in reg_set:
            continue
        meta = db.get_item("service_meta", row.get("service_meta_id")) or {}
        if "singleton" in (meta.get("tags") or []):
            continue
        db.delete_item("service_proxy", rid)
        actions[rid] = "pruned:not_in_registry"
    return actions


# ─── orchestrator ─────────────────────────────────────────────────────


def boot_from_config_set(
    db,
    repo_dir: Path,
    data_dir: Path,
) -> Dict[str, Any]:
    """Migrate (if needed) + sync. Returns a report dict for logging.

    Caller (event_handlers.on_startup) calls this BEFORE
    reconcile_running_proxies. After this, the rows in ``service_proxy``
    reflect the contents of the active config set on disk; reconcile
    spawns whatever is in start_order.
    """
    manifests = {m.id: m for m in scan_repos(_all_roots(repo_dir))}
    set_dir = active_set_dir(data_dir)
    set_dir.mkdir(parents=True, exist_ok=True)

    sec_core = bootstrap_security_core(data_dir, repo_dir=repo_dir)
    encrypt_fn = sec_core.encrypt if sec_core else None
    decrypt_fn = sec_core.decrypt if sec_core else None

    migrated: List[str] = []
    if _set_is_empty(set_dir):
        migrated = migrate_db_to_config_set(
            db, set_dir, manifests, repo_dir, encrypt_fn=encrypt_fn,
        )
        if migrated:
            logger.info(
                "boot.migrate: wrote %d yml file(s) to %s (set name=%s)",
                len(migrated), set_dir, active_set_name(),
            )

    # The registry is the authoritative, ordered membership. An explicit
    # runtime.registry wins; an empty/absent one backfills from the ymls
    # (glob-equivalent) + persists, so the set is registry-driven henceforth.
    registry = _effective_registry(set_dir, load_runtime_yml(set_dir))
    actions = sync_config_set_to_db(db, set_dir, manifests, registry, decrypt_fn=decrypt_fn)
    missing = [k for k, v in actions.items() if v == "error:missing_yml"]
    if actions:
        created = [k for k, v in actions.items() if v == "created"]
        updated = [k for k, v in actions.items() if v == "updated"]
        pruned = [k for k, v in actions.items() if v == "pruned:not_in_registry"]
        skipped = {k: v for k, v in actions.items() if v.startswith("skipped:")}
        errored = {k: v for k, v in actions.items() if v.startswith("error:")}
        logger.info(
            "boot.sync: set=%s created=%d updated=%d pruned=%d skipped=%d error=%d",
            active_set_name(), len(created), len(updated), len(pruned),
            len(skipped), len(errored),
        )
        for k, reason in {**skipped, **errored}.items():
            logger.warning("boot.sync: %s %s", k, reason)

    # Machine-readable feed for the UI (retained): the resolved registry +
    # any registry name whose yml was missing (surfaced as a visible error).
    try:
        from botcore.runtime.bus import get_bus
        get_bus().publish_sync(
            "/system/config_set/report",
            {"set": active_set_name(), "registry": registry,
             "missing_yml": missing, "sync_actions": actions},
            retained=True,
        )
    except Exception:  # noqa: BLE001
        logger.debug("config_set report publish failed", exc_info=True)

    return {
        "set_name": active_set_name(),
        "set_dir": str(set_dir),
        "migrated": migrated,
        "registry": registry,
        "missing_yml": missing,
        "sync_actions": actions,
        "security_available": sec_core is not None,
    }


# ─── snapshot (DB + live state → yml) ─────────────────────────────────


def _save_one_proxy(
    row: Dict[str, Any],
    manifests: Dict[str, Any],
    set_dir: Path,
    encrypt_fn: Optional[Callable[[str], str]],
    repo_dir: Path,
    prefer_live: bool = True,
) -> Optional[str]:
    """Snapshot ONE proxy row to ``<set_dir>/<proxy_id>.yml``.

    Returns the ``desired_state`` written (``'running'`` / ``'stopped'``)
    so the caller can aggregate, or ``None`` if the row was a singleton
    and skipped. Raises on persistence failure — callers decide whether
    to log + continue (save_all) or surface as 404/500 (save_one).

    ``prefer_live`` (default True — the drain / per-node-save path) reads a
    LIVE in-process Service's ``self.config`` so flushed runtime state
    (clock ``is_clock_running`` etc.) is captured. The AUTO-SAVE path
    (``lifecycle.autosave_service_config`` after a link bind / subprocess
    config_patch) passes ``False``: the ROW holds the just-mutated truth,
    while the live object may not have applied the just-broadcast
    ``config_state`` yet — reading it would persist stale config.
    """
    proxy_id = row.get("id")
    type_id = row.get("service_meta_id")
    if not proxy_id or not type_id:
        return None
    from botcore import framework  # lazy: avoid import cycle at module load

    status = row.get("status")
    desired = "running" if status in ("running", "starting", "installing") else "stopped"

    # Prefer the live Service object's config so freshly-flushed runtime
    # state (clock is_clock_running, etc.) is captured. Fall back to the
    # row's stored config when the service isn't running in-process — or
    # always, when prefer_live is False (auto-save reads the row, not a
    # live object that may not have caught up to the broadcast yet).
    handle = framework.REGISTRY.get(proxy_id) if prefer_live else None
    svc = handle.payload.get("service") if handle is not None else None
    manifest = manifests.get(type_id)

    if svc is not None:
        try:
            svc.serialize_runtime_state()
        except Exception:  # noqa: BLE001 — never block the snapshot
            logger.exception(
                "boot.save: serialize_runtime_state failed for %s", proxy_id,
            )
        save_proxy_yml(
            set_dir, proxy_id, type_id, svc.config,
            encrypt_fn=encrypt_fn, desired_state=desired,
        )
    else:
        # Not live in-process. Config now lives in the yml (stone 4b: the row
        # no longer stores it), so only refresh desired_state, preserving the
        # on-disk (encrypted) config untouched. If there's no yml yet (a
        # never-saved proxy mid-migration), fall back to the row's transition
        # config so nothing is lost.
        if not _set_desired_state_in_yml(set_dir, proxy_id, desired):
            raw = row.get("service_config") or {}
            if manifest is not None:
                try:
                    config_instance = _row_to_config_instance(row, manifest, repo_dir)
                    save_proxy_yml(
                        set_dir, proxy_id, type_id, config_instance,
                        encrypt_fn=encrypt_fn, desired_state=desired,
                    )
                except Exception:  # noqa: BLE001 — class unloadable: raw write
                    _write_raw_yml(
                        set_dir, proxy_id, type_id, raw,
                        encrypt_fn=encrypt_fn, desired_state=desired,
                    )
            else:
                _write_raw_yml(
                    set_dir, proxy_id, type_id, raw,
                    encrypt_fn=encrypt_fn, desired_state=desired,
                )
    return desired


def save_one_service_config(
    db,
    repo_dir: Path,
    data_dir: Path,
    proxy_id: str,
    prefer_live: bool = True,
) -> Dict[str, Any]:
    """Snapshot a SINGLE service proxy's config to the booted set's yml.

    Counterpart to ``save_all_service_config``. Used by the per-node
    "save" affordance in the Composer title bar so the operator can
    commit one service's tweaks (e.g. an IK model edit) without
    rewriting every other ``*.yml`` on disk.

    ``prefer_live=False`` makes this save the DB ROW's config rather than a
    live Service's — the auto-save path uses it right after an external
    mutation (see ``_save_one_proxy``).
    """
    row = db.get_item("service_proxy", proxy_id)
    if not row:
        return {"ok": False, "error": f"unknown proxy_id: {proxy_id}"}
    type_id = row.get("service_meta_id")
    if not type_id:
        return {"ok": False, "error": f"proxy {proxy_id} has no service_meta_id"}
    meta = db.get_item("service_meta", type_id) or {}
    if "singleton" in (meta.get("tags") or []):
        return {
            "ok": False,
            "skipped": "singleton (rides restart — no per-proxy save)",
            "proxy_id": proxy_id,
        }

    manifests = {m.id: m for m in scan_repos(_all_roots(repo_dir))}
    set_dir = active_set_dir(data_dir)
    set_dir.mkdir(parents=True, exist_ok=True)
    sec_core = bootstrap_security_core(data_dir, repo_dir=repo_dir)
    encrypt_fn = sec_core.encrypt if sec_core else None

    try:
        desired = _save_one_proxy(row, manifests, set_dir, encrypt_fn, repo_dir, prefer_live=prefer_live)
    except Exception as exc:  # noqa: BLE001
        logger.exception("boot.save_one: failed to snapshot %s", proxy_id)
        return {"ok": False, "proxy_id": proxy_id, "error": f"{type(exc).__name__}: {exc}"}

    logger.info(
        "boot.save_one: set=%s proxy=%s desired=%s",
        active_set_name(), proxy_id, desired,
    )
    return {
        "ok": True,
        "set_name": active_set_name(),
        "set_dir": str(set_dir),
        "proxy_id": proxy_id,
        "yml_path": str(set_dir / f"{proxy_id}.yml"),
        "desired_state": desired,
    }


def save_all_service_config(
    db,
    repo_dir: Path,
    data_dir: Path,
) -> Dict[str, Any]:
    """Snapshot every managed service into the BOOTED set's ymls so the
    exact current state is restored on the next boot.

    For each ``service_proxy`` row this records:

      * ``desired_state`` — ``running`` if the row is live
        (running/starting/installing), else ``stopped``. boot.sync reads
        this back to decide start-vs-create-only.
      * The service's config, including any runtime state the service
        flushes via ``serialize_runtime_state`` (e.g. the clock writes
        ``is_clock_running`` so a ticking clock comes back ticking). For a
        LIVE in-process service we snapshot its live ``self.config``; for a
        non-running one we reconstruct from the row's stored config.

    Singletons (runtime, security) are skipped — they ride the restart with
    the process and re-materialize on boot, exactly like drain leaves them
    alone. Writing to the pinned booted set means a staged config switch
    can't retarget these writes at another set.

    Safe to call on demand (a UI/operator "save all" button) or just before
    a graceful drain. Returns a report dict for logging.
    """
    manifests = {m.id: m for m in scan_repos(_all_roots(repo_dir))}
    set_dir = active_set_dir(data_dir)
    set_dir.mkdir(parents=True, exist_ok=True)

    sec_core = bootstrap_security_core(data_dir, repo_dir=repo_dir)
    encrypt_fn = sec_core.encrypt if sec_core else None

    saved: List[str] = []
    skipped: Dict[str, str] = {}
    live_states: Dict[str, str] = {}
    row_ids: List[str] = []          # non-singleton rows in get_all_items order

    for row in (db.get_all_items("service_proxy") or []):
        proxy_id = row.get("id")
        type_id = row.get("service_meta_id")
        if not proxy_id or not type_id:
            continue
        meta = db.get_item("service_meta", type_id) or {}
        if "singleton" in (meta.get("tags") or []):
            skipped[proxy_id] = "singleton (rides restart)"
            continue
        row_ids.append(proxy_id)
        try:
            desired = _save_one_proxy(row, manifests, set_dir, encrypt_fn, repo_dir)
            if desired is not None:
                live_states[proxy_id] = desired
                saved.append(proxy_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("boot.save: failed to snapshot %s", proxy_id)
            skipped[proxy_id] = f"{type(exc).__name__}: {exc}"

    # Persist the ordered registry (decision #5): the prior registry order for
    # survivors (keeps a hand-curated order + any missing-yml entry that's still
    # a deliberate member) + any newcomer rows appended in row order. Derived
    # from ROWS + prior registry — never a directory glob (a glob would
    # resurrect uninstalled services + revert registry-only removals). Removal
    # happens only via uninstall (drops the name) or a registry edit.
    try:
        prev = load_runtime_yml(set_dir).registry
        new_order = list(prev) + [rid for rid in row_ids if rid not in prev]
        write_runtime_registry(set_dir, new_order)
    except Exception:  # noqa: BLE001 — never abort save on a registry-write hiccup
        logger.exception("boot.save: registry persist failed for %s", set_dir)

    logger.info(
        "boot.save: set=%s saved=%d skipped=%d (running=%d stopped=%d)",
        active_set_name(), len(saved), len(skipped),
        sum(1 for v in live_states.values() if v == "running"),
        sum(1 for v in live_states.values() if v == "stopped"),
    )
    return {
        "ok": True,
        "set_name": active_set_name(),
        "set_dir": str(set_dir),
        "saved": saved,
        "skipped": skipped,
        "desired_states": live_states,
    }
