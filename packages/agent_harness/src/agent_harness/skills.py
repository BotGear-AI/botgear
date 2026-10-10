"""Agent Skills folders: validate, list, and share their scripts.

A skill is a folder with a SKILL.md whose YAML frontmatter has `name`
(kebab-case, equal to the folder name) and a non-empty `description`,
plus optional reference.md, scripts/ and assets/. Extra frontmatter keys
are allowed (callers may use them, e.g. to describe required inputs).

Skill search paths are ordered: the first folder that has a skill of a
given name wins. That makes per-user copies trivial, e.g.
`[user_skills_dir, library_dir]` lets a user's fork shadow the library
version without any extra logic.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable

from agent_harness.workflow import WorkflowError, parse_frontmatter

KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SKILL_FILE = "SKILL.md"


def is_safe_relpath(rel: str) -> bool:
    """True if `rel` stays inside its folder (not absolute, no `..`)."""
    p = Path(rel)
    return not p.is_absolute() and ".." not in p.parts


def is_skill_dir(path: Path) -> bool:
    """Folders starting with '.' or '_' are shared tooling, never skills."""
    return path.is_dir() and not path.name.startswith((".", "_")) and (path / SKILL_FILE).is_file()


def validate_skill_dir(skill_dir: Path) -> list[str]:
    """Return a list of problems with one skill folder ([] means valid)."""
    skill_dir = Path(skill_dir)
    errors: list[str] = []
    skill_md = skill_dir / SKILL_FILE
    if not skill_md.is_file():
        return [f"{SKILL_FILE} missing"]
    try:
        meta, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    except WorkflowError as exc:
        return [str(exc)]
    if not meta:
        return [f"{SKILL_FILE} has no YAML frontmatter block"]
    name = meta.get("name")
    if not name:
        errors.append("frontmatter 'name' is required")
    else:
        if not KEBAB.match(str(name)):
            errors.append(f"name {name!r} must be kebab-case")
        if str(name) != skill_dir.name:
            errors.append(f"name {name!r} must equal the folder name {skill_dir.name!r}")
    if not str(meta.get("description") or "").strip():
        errors.append("frontmatter 'description' is required")
    root = skill_dir.resolve()
    for path in skill_dir.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(root):
            errors.append(f"{path.relative_to(skill_dir)} links outside the skill folder")
    return errors


def skill_metadata(skill_dir: Path) -> dict[str, Any]:
    """The skill's frontmatter plus its folder name (for list views)."""
    meta, _ = parse_frontmatter((Path(skill_dir) / SKILL_FILE).read_text(encoding="utf-8"))
    return {**(meta or {}), "name": Path(skill_dir).name}


def list_skills(search_paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Every skill visible through the ordered search paths, first match
    wins. Each entry is its frontmatter plus `path`, `source` (the search
    path it came from) and `shadows` (later paths it hides)."""
    found: dict[str, dict[str, Any]] = {}
    for base in [Path(p) for p in search_paths]:
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not is_skill_dir(child):
                continue
            if child.name in found:
                found[child.name]["shadows"].append(str(base))
                continue
            try:
                meta = skill_metadata(child)
            except WorkflowError:
                meta = {"name": child.name}
            found[child.name] = {**meta, "path": str(child), "source": str(base), "shadows": []}
    return [found[name] for name in sorted(found)]


def import_skill_script(skill_dir: Path, relpath: str) -> ModuleType:
    """Import one of a skill's scripts as a module, so application code
    and checks use the exact logic the agent runs (one source of truth)."""
    if not is_safe_relpath(relpath):
        raise ValueError(f"unsafe script path: {relpath!r}")
    path = Path(skill_dir) / relpath
    spec = importlib.util.spec_from_file_location(f"_skill_{Path(skill_dir).name}_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
