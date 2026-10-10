"""Build the per-run workspace an agent runs inside.

Layout (all under `<workspace_root>/<run_name>/`):

  AGENTS.md            copied from the caller's content (repo invariants)
  task.md              the rendered workflow contract
  workflow/            COPY of the workflow folder (stage notes, templates)
  inputs/<name>        symlinks to path inputs (read-only by policy)
  .claude/skills/<n>/  COPIES of the resolved skills (a snapshot, so a run is
  .agents/skills/<n>/  reproducible even if the skill changes later)
  output/              the only place the agent writes
  state.json, events.jsonl, checks.json, result.json   written by the harness

The workspace is initialized as its own (empty) git repository. Agent
harnesses treat the git root as the project boundary when discovering
instructions and skills, so without this a workspace that happens to sit
inside another repository would pick up that repository's AGENTS.md,
CLAUDE.md and skills.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_SKILL_DIRS = (".claude/skills", ".agents/skills")
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


@dataclass
class Workspace:
    root: Path

    @property
    def output(self) -> Path:
        return self.root / "output"

    @property
    def inputs(self) -> Path:
        return self.root / "inputs"

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def task_path(self) -> Path:
        return self.root / "task.md"

    def skill_dir(self, name: str) -> Path:
        return self.root / _SKILL_DIRS[0] / name


def make_run_name(workflow: str, agent: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return _UNSAFE.sub("-", f"{stamp}-{workflow}-{agent}")


def build_workspace(
    workspace_root: Path,
    run_name: str,
    *,
    inputs: dict[str, Any],
    path_inputs: set[str],
    skills: dict[str, Path],
    agents_md: str,
    workflow_dir: Path | None = None,
) -> Workspace:
    root = Path(workspace_root) / run_name
    if root.exists():
        raise FileExistsError(f"workspace already exists: {root}")
    ws = Workspace(root=root)
    ws.output.mkdir(parents=True)
    ws.inputs.mkdir()

    for name in path_inputs:
        if name in inputs:
            _link_input(ws.inputs / name, Path(inputs[name]))

    for rel in _SKILL_DIRS:
        for name, src in skills.items():
            shutil.copytree(src, root / rel / name, ignore=_IGNORE)

    if workflow_dir is not None:
        # Checks run from the original folder (by the harness), never this copy.
        shutil.copytree(workflow_dir, root / "workflow", ignore=_IGNORE)
    if agents_md:
        (root / "AGENTS.md").write_text(agents_md, encoding="utf-8")
    _make_git_boundary(root)
    return ws


def prune_workspaces(workspace_root: Path, keep: int, match: str = "*") -> list[Path]:
    """Delete all but the newest `keep` workspaces under `workspace_root`
    whose names match the glob `match` (default: all), by name, which starts
    with a sortable timestamp. E.g. keep the last 5 per workflow and agent
    with match="????????-??????-<workflow>-<agent>" (make_run_name's shape).
    Returns what was removed. Callers decide when to call this; nothing
    prunes implicitly."""
    root = Path(workspace_root)
    if keep < 0 or not root.is_dir():
        return []
    runs = sorted((p for p in root.glob(match) if p.is_dir() and (p / "state.json").exists()),
                  key=lambda p: p.name)
    doomed = runs[:-keep] if keep else runs
    for path in doomed:
        shutil.rmtree(path, ignore_errors=True)
    return doomed


def _link_input(link: Path, target: Path) -> None:
    """Symlink `link` -> `target`, relative to the link's directory.

    Relative, so the link resolves wherever the tree is mounted: a workspace
    on a network mount (e.g. sshfs) is created on the remote host, where an
    absolute path from this machine doesn't exist. Some network filesystems
    (sshfs with follow_symlinks) also report an error after creating the link
    successfully, so an error is only real if the link doesn't resolve.
    """
    target = target.resolve()
    relative = os.path.relpath(target, link.parent.resolve())
    try:
        link.symlink_to(relative, target_is_directory=target.is_dir())
    except OSError:
        if not link.exists():
            raise
        logger.debug("symlink %s -> %s reported an error but resolves; continuing", link, relative)


def _make_git_boundary(root: Path) -> None:
    try:
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not git-init workspace %s (agents may see parent-repo context): %s", root, exc)
