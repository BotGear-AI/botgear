# unmanaged
"""Framework self-update — APPLY (download → verify → stage → carry var → swap → restart).

MVP for docs/TODO_UPGRADE_FEATURE.md §5 (Phase 4 happy path). A frozen instance
replaces its own framework with a newer release and re-execs into it, carrying
user state (``var/``) across the swap. It REUSES the tested restart backbone:
stage the new bundle beside the current one, copy ``var/`` into it, rewrite the
recorded start command's ``argv[0]`` to the new binary, drain, then
``system.restart()`` execs the new binary.

On-disk (flat bundle — the A/B ``current`` symlink layout is the Phase-3
hardening, not this MVP)::

    <parent>/botgear-<cur>-<plat>/   running install  (binary + var/)
    <parent>/botgear-<new>-<plat>/   staged new install (extracted)
    <parent>/.rlx-update/               download scratch
    <parent>/rlx-backups/<cur>-<ts>/    pre-swap var/ backup (rollback image)

SAFETY: download/verify/extract are reversible (just delete the scratch + new
dir). The point of no return is the command rewrite + execv. We sha256-verify
the tarball against its sidecar, guard tar extraction against path traversal,
DRAIN services before copying ``var/`` (TinyDB is a plain JSON file — copying it
mid-write risks a torn read), and keep BOTH the old install and a ``var/``
backup so an operator can roll back manually. Automatic boot-failure rollback
(a supervisor/watchdog) is Phase 5 — NOT in this MVP; a failed boot needs manual
recovery (repoint the old binary).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path
from typing import Callable, Optional

from botgear.runtime.updater import platform_tag

logger = logging.getLogger(__name__)

# publish(phase, pct_or_None, human_message)
ProgressCb = Callable[[str, Optional[int], str], None]

_IGNORE = shutil.ignore_patterns("logs", "*.log", "last_start_command.json")


class UpdateError(RuntimeError):
    """A user-meaningful update failure (mismatch, bad layout, dev build, …)."""


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_root() -> Path:
    if not is_frozen():
        raise UpdateError("self-update is only available in a packaged build (not a dev source run)")
    return Path(sys.executable).resolve().parent


def current_version() -> str:
    try:
        return json.loads((install_root() / "version.json").read_text()).get("version") or "0.0.0"
    except Exception:  # noqa: BLE001
        return "0.0.0"


def _command_file() -> Path:
    from config import get_settings
    d = Path(getattr(get_settings(), "data_dir", None) or "data")
    if not d.is_absolute():
        d = Path.cwd() / d
    return d / "last_start_command.json"


# ── reversible staging ────────────────────────────────────────────────────

# stdlib urllib (NOT httpx) so the download works inside the frozen bundle,
# where lazily-imported third-party deps aren't collected by PyInstaller.
_UA = "botgear-updater"


def _download_sync(url: str, dest: Path, progress: ProgressCb) -> str:
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    written = 0
    last_pct = -1
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as f:  # noqa: S310 — fixed https asset URL
        total = int(resp.headers.get("Content-Length") or 0)
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            written += len(chunk)
            if total:
                pct = int(written * 100 / total)
                if pct != last_pct:
                    last_pct = pct
                    progress("download", pct, f"downloading… {written >> 20}/{total >> 20} MB")
    return h.hexdigest()


async def _download(url: str, dest: Path, progress: ProgressCb) -> str:
    """Stream ``url`` → ``dest`` off the event loop; return the sha256 written."""
    return await asyncio.to_thread(_download_sync, url, dest, progress)


def _expected_sha_sync(sha_url: str) -> str:
    import urllib.request

    req = urllib.request.Request(sha_url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed https sidecar URL
        return resp.read().decode("utf-8").strip().split()[0].lower()


async def _expected_sha(sha_url: str) -> str:
    return await asyncio.to_thread(_expected_sha_sync, sha_url)


def _safe_extract(tarball: Path, dest_parent: Path) -> Path:
    """Extract ``tarball`` under ``dest_parent`` with a path-traversal guard.
    Returns the single top-level directory it created."""
    root = dest_parent.resolve()
    with tarfile.open(tarball, "r:gz") as tf:
        members = tf.getmembers()
        for m in members:
            target = (dest_parent / m.name).resolve()
            if target != root and root not in target.parents:
                raise UpdateError(f"unsafe path in archive: {m.name!r}")
        tf.extractall(dest_parent)
    tops = {Path(m.name).parts[0] for m in members if m.name and not m.name.startswith(("/", ".."))}
    if len(tops) != 1:
        raise UpdateError(f"unexpected archive layout (top-level dirs: {sorted(tops)})")
    return dest_parent / next(iter(tops))


# ── point of (near) no return: carry var + repoint + restart ──────────────

def _copytree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True, ignore=_IGNORE)


def _carry_var(cur_install: Path, new_install: Path, backup_dir: Path, progress: ProgressCb) -> None:
    cur_var = cur_install / "var"
    new_var = new_install / "var"
    progress("backup", None, "backing up your data…")
    _copytree(cur_var, backup_dir / "var")
    progress("carry", None, "carrying your data to the new version…")
    _copytree(cur_var, new_var)
    # Carry the operator .env (port / JWT secret / data dir) if they made one.
    env = cur_install / ".env"
    if env.is_file():
        shutil.copy2(env, new_install / ".env")


def _point_command_at(new_install: Path) -> None:
    """Rewrite the recorded start command's argv[0]/cwd to the new binary so
    the tested restart() execs the NEW framework. The new process re-records
    its own command on boot, so we only need the copy restart() reads now."""
    new_binary = str(new_install / "botgear")
    cf = _command_file()
    try:
        data = json.loads(cf.read_text())
    except Exception:  # noqa: BLE001 — fall back to a minimal command
        data = {}
    argv = list(data.get("argv") or [])
    if argv:
        argv[0] = new_binary
    else:
        argv = [new_binary]
    data.update(argv=argv, cwd=str(new_install / "var"), executable=new_binary)
    cf.write_text(json.dumps(data, indent=2))


async def run_update(target_version: str, asset_url: str, sha256_url: str, *, publish: ProgressCb) -> None:
    """Apply a framework update end to end. Returns only on FAILURE (success
    ends in execv, which never returns). Publishes phase/progress throughout."""
    cur_install = install_root()
    parent = cur_install.parent
    ptag = platform_tag()
    new_install = parent / f"botgear-{target_version}-{ptag}"
    scratch = parent / ".rlx-update"
    tarball = scratch / f"botgear-{target_version}-{ptag}.tar.gz"
    backup_dir = parent / "rlx-backups" / f"{current_version()}-{time.strftime('%Y%m%d-%H%M%S')}"

    try:
        if not asset_url or not sha256_url:
            raise UpdateError(f"no downloadable {ptag} asset for {target_version}")
        publish("start", 0, f"updating to {target_version}…")

        # 1. download + verify (reversible)
        got = await _download(asset_url, tarball, publish)
        publish("verify", None, "verifying download…")
        want = await _expected_sha(sha256_url)
        if got.lower() != want:
            raise UpdateError(f"sha256 mismatch (expected {want[:12]}…, got {got[:12]}…)")

        # 2. stage (reversible)
        publish("extract", None, "unpacking the new version…")
        if new_install.exists():
            shutil.rmtree(new_install)
        extracted = _safe_extract(tarball, parent)
        if extracted != new_install:
            if new_install.exists():
                shutil.rmtree(new_install)
            extracted.rename(new_install)
        staged_ver = json.loads((new_install / "version.json").read_text()).get("version")
        if staged_ver != target_version:
            raise UpdateError(f"staged bundle is {staged_ver!r}, expected {target_version!r}")
        if not (new_install / "botgear").exists():
            raise UpdateError("staged bundle has no botgear binary")

        # 3. quiesce — drain services + reconciler so var/ (TinyDB) is at rest
        from botgear.runtime import system, lifecycle
        system.set_draining(True)
        publish("drain", None, "stopping services…")
        try:
            lifecycle.save_all_service_config()
            lifecycle.drain_services()
        except Exception:  # noqa: BLE001 — never block the update on a drain hiccup
            logger.exception("update: drain hiccup (continuing)")
        await asyncio.sleep(1.0)  # let final DB writes settle

        # 4. carry data + repoint (the point of no return)
        _carry_var(cur_install, new_install, backup_dir, publish)
        _point_command_at(new_install)

        # 5. restart into the new binary (execs — never returns)
        publish("restart", 100, f"restarting into {target_version}…")
        await asyncio.sleep(1.0)  # flush the final progress frame before execv
        system.restart()
    except Exception as exc:  # noqa: BLE001
        logger.exception("update: failed")
        try:
            from botgear.runtime import system
            system.set_draining(False)
        except Exception:  # noqa: BLE001
            pass
        publish("failed", None, f"update failed: {exc}")
        # Leave the old install running + intact; clean only the scratch.
        shutil.rmtree(scratch, ignore_errors=True)
        raise
