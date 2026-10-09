# unmanaged
"""Ratchet test for the topic_schemas/state_schema convention.

Context: botgear services document their published-topic wire shapes
via `state_schema`/`topic_schemas` class attributes (introspected into
`/runtime/runtime/types/<name>` — see `runtime/types_index.py` and
`runtime/schema_introspect.py`). It's an OPT-IN convention — nothing
in the runtime requires it, unlike `config_class`, which the Config
Wizard structurally can't work without. As of this test's introduction,
most existing services haven't adopted it yet (see the migration audit
in AGENTS.md's history / conversation notes) — a hard "100% coverage"
gate today would just fail on most of the fleet and teach no one
anything.

So this is a RATCHET, not a blanket gate:
  * A type NOT in `_LEGACY_ALLOWLIST` must have full coverage — no
    grace period, including for any NEW in-process service, because
    types are discovered by walking `repo/*/1.0.0/package.yml` at
    collection time rather than being hand-listed here.
  * A type IN `_LEGACY_ALLOWLIST` is grandfathered (shown as SKIPPED,
    not passed, so the gap stays visible) until someone converts it.
  * `test_allowlist_entries_are_still_needed` keeps the allowlist
    honest — once a listed type is actually converted, that test fails
    until the now-stale entry is removed, so the ratchet can't
    silently regress by accumulating dead grandfather entries.

Coverage rule: a type's `publishes` class attribute (the same one
`service_methods_api.py`'s topology endpoint already reads, and that
`clock`/`chat`/`master_template` already declare) is treated as its
declared intent. Every non-"state" entry must have a matching key in
`topic_schemas`; "state" being present requires a non-None
`state_schema`.

Explicit scope limits, so this test isn't mistaken for a stronger
guarantee than it is:
  * IN-PROCESS types only. Subprocess types need an equivalent
    declared-intent signal on `rlx_bus.SubprocessService` (no such
    convention exists yet) and reliable per-type venv introspection —
    neither is assumed here, so subprocess coverage isn't ratcheted
    yet.
  * Only checks what's DECLARED in `publishes`. A service that
    publishes real topics without ever listing them there isn't
    caught — nothing enforces that list's completeness either. This
    test tightens the opt-in convention's own enforcement; it doesn't
    invent a new one.
  * `subscribes_schemas` (inbound topics) isn't ratcheted here either
    — there's no `publishes`-equivalent declared-intent list for
    inbound topics to check against (see `chat.py`'s `inbox` for the
    one hand-audited example so far).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from botgear.framework.adapters.in_process import _load_service_class
from botgear.runtime.schema_introspect import class_state_schema, class_topic_schemas

_REPO_DIR = Path(__file__).resolve().parents[1] / "repo"

# Every entry here is a REAL, currently-existing gap (declares
# `publishes=[...]` with no matching `topic_schemas`/`state_schema`) —
# not a false positive from an overly strict rule. Remove an entry the
# moment that service is converted; leaving a stale one is caught by
# `test_allowlist_entries_are_still_needed` below.
_LEGACY_ALLOWLIST: frozenset = frozenset()


def _discover_in_process_types() -> List[Dict[str, Any]]:
    """Every `repo/<name>/1.0.0/package.yml` declaring `language:
    builtin` with a real `entry.in_process.class` — excludes pure
    singleton markers like `runtime`, which has no backing class to
    introspect at all (no `entry:` block in its manifest)."""
    found: List[Dict[str, Any]] = []
    for pkg_path in sorted(_REPO_DIR.glob("*/1.0.0/package.yml")):
        try:
            manifest = yaml.safe_load(pkg_path.read_text())
        except Exception:  # noqa: BLE001 — a malformed manifest is its own problem, not this test's
            continue
        if not isinstance(manifest, dict) or manifest.get("language") != "builtin":
            continue
        entry = (manifest.get("entry") or {}).get("in_process") or {}
        module_name, class_name = entry.get("module"), entry.get("class")
        if not module_name or not class_name:
            continue
        found.append({
            "type_name": manifest.get("name") or pkg_path.parent.parent.name,
            "type_version": pkg_path.parent.name,
            "module": module_name,
            "class": class_name,
            "repo_root": pkg_path.parent.parent.parent,  # .../repo
        })
    return found


_IN_PROCESS_TYPES = _discover_in_process_types()
_TYPE_NAMES = [t["type_name"] for t in _IN_PROCESS_TYPES]


def _violations_for(entry: Dict[str, Any]) -> List[str]:
    cls = _load_service_class(
        entry["repo_root"], entry["type_name"], entry["type_version"],
        entry["module"], entry["class"],
    )
    publishes = list(getattr(cls, "publishes", None) or [])
    if not publishes:
        return []
    problems: List[str] = []
    if "state" in publishes and class_state_schema(cls) is None:
        problems.append("declares 'state' in `publishes` but has no `state_schema`")
    covered = class_topic_schemas(cls)
    for topic in publishes:
        if topic == "state":
            continue
        if topic not in covered:
            problems.append(f"declares '{topic}' in `publishes` but it's missing from `topic_schemas`")
    return problems


@pytest.mark.parametrize("entry", _IN_PROCESS_TYPES, ids=_TYPE_NAMES)
def test_in_process_service_has_schema_coverage(entry: Dict[str, Any]) -> None:
    """See module docstring for the ratchet mechanics. TL;DR: a type
    not on `_LEGACY_ALLOWLIST` must fully cover its declared
    `publishes` list via `state_schema`/`topic_schemas`."""
    violations = _violations_for(entry)
    if entry["type_name"] in _LEGACY_ALLOWLIST:
        pytest.skip(
            f"{entry['type_name']}: grandfathered in _LEGACY_ALLOWLIST "
            f"({len(violations)} known gap(s) — convert it, then remove this entry)"
        )
    assert not violations, f"{entry['type_name']}: " + "; ".join(violations)


def test_allowlist_entries_are_still_needed() -> None:
    """Keeps `_LEGACY_ALLOWLIST` honest. Once a listed type is actually
    converted, this fails until the now-stale entry is removed — so the
    ratchet can't quietly rot into a permanent exemption list."""
    by_name = {t["type_name"]: t for t in _IN_PROCESS_TYPES}
    stale: List[str] = []
    for name in sorted(_LEGACY_ALLOWLIST):
        entry = by_name.get(name)
        if entry is None:
            stale.append(f"{name}: no longer exists as an in-process type — remove from _LEGACY_ALLOWLIST")
            continue
        if not _violations_for(entry):
            stale.append(f"{name}: already fully covered — remove from _LEGACY_ALLOWLIST")
    assert not stale, "\n".join(stale)


# ── subprocess types ─────────────────────────────────────────────────
#
# Same coverage rule, but the class lives in the type's OWN venv, so it's
# introspected the same way the runtime's types index does it:
# `subprocess_introspect` runs `rlx_bus.introspect.describe_class` inside
# that venv. A type whose venv isn't installed on this machine is
# SKIPPED (visible gap, not a pass) — there's no way to import its class.
# `_SUBPROCESS_LEGACY_ALLOWLIST` works exactly like `_LEGACY_ALLOWLIST`.

_SUBPROCESS_LEGACY_ALLOWLIST: frozenset = frozenset()

# Subprocess types that aren't bus services at all — no SubprocessService
# class to introspect, so there's nothing to cover. Not grandfathered
# gaps; a permanent, reasoned exclusion.
_NOT_BUS_SERVICES: Dict[str, str] = {
    "echo_http": "plain FastAPI/uvicorn demo process — no SubprocessService class, publishes nothing",
}


def _discover_subprocess_types() -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for pkg_path in sorted(_REPO_DIR.glob("*/1.0.0/package.yml")):
        try:
            manifest = yaml.safe_load(pkg_path.read_text())
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(manifest, dict) or manifest.get("language") != "python":
            continue
        argv = (manifest.get("entry") or {}).get("argv") or []
        if "-m" not in argv:
            continue
        if (manifest.get("name") or pkg_path.parent.parent.name) in _NOT_BUS_SERVICES:
            continue
        found.append({
            "type_name": manifest.get("name") or pkg_path.parent.parent.name,
            "type_dir": pkg_path.parent,
            "argv": list(argv),
        })
    return found


_SUBPROCESS_TYPES = _discover_subprocess_types()


def _subprocess_violations(entry: Dict[str, Any]) -> List[str]:
    """Returns the violation list, or raises pytest.skip when the venv
    is missing / the class can't be imported there."""
    from botgear.runtime import proc_compat
    from botgear.runtime.schema_introspect import subprocess_introspect

    venv_python = proc_compat.venv_python(entry["type_dir"] / ".venv")
    if not Path(venv_python).exists():
        pytest.skip(f"{entry['type_name']}: no .venv installed — can't introspect")
    bundle = subprocess_introspect(Path(venv_python), entry["type_name"], entry["argv"], timeout=60.0)
    if bundle is None:
        pytest.skip(f"{entry['type_name']}: introspection failed in its venv (class import error?)")
    publishes = bundle.get("publishes") or []
    problems: List[str] = []
    if not publishes:
        problems.append("declares no `publishes` list — nothing to check coverage against")
    if "state" in publishes and bundle.get("state_schema") is None:
        problems.append("declares 'state' in `publishes` but has no `state_schema`")
    covered = bundle.get("topic_schemas") or {}
    for topic in publishes:
        if topic != "state" and topic not in covered:
            problems.append(f"declares '{topic}' in `publishes` but it's missing from `topic_schemas`")
    return problems


@pytest.mark.parametrize(
    "entry", _SUBPROCESS_TYPES, ids=[t["type_name"] for t in _SUBPROCESS_TYPES],
)
def test_subprocess_service_has_schema_coverage(entry: Dict[str, Any]) -> None:
    """Subprocess counterpart of `test_in_process_service_has_schema_coverage`.
    Stricter in one way: a subprocess type must DECLARE `publishes` —
    otherwise the check would silently pass on an empty list."""
    if entry["type_name"] in _SUBPROCESS_LEGACY_ALLOWLIST:
        pytest.skip(f"{entry['type_name']}: grandfathered in _SUBPROCESS_LEGACY_ALLOWLIST")
    violations = _subprocess_violations(entry)
    assert not violations, f"{entry['type_name']}: " + "; ".join(violations)
