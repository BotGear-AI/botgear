# unmanaged
"""Runtime-level type catalog — one descriptor per registered service type.

Where ``runtime/services_index.py`` lists every running *instance*,
this module publishes a parallel index of every registered *type* so a
consumer can answer the questions ``services_index`` can't:

  * What types could exist on this runtime?
  * What does a ``video`` service's config look like before I create one?
  * What does its ``state`` payload look like before I subscribe?
  * What arguments does its ``add_filter`` action accept?
  * What shape does a ``chat`` instance expect on the topic I'd publish
    to feed it (``subscribes_schemas``)?

Topics published (all retained):

    /runtime/runtime/types                       summary list — every registered type
    /runtime/runtime/types/<type_name>           full descriptor per type (the schema home)

Type-name = the repo directory name = the wire key. There is exactly one
canonical schema document per type-name; instances reference it by key
rather than carrying their own copy.

Sources for the descriptor:
  * ``service_meta`` DB rows — manifest-derived fields (description,
    tags, transport, entry_*, install_*).
  * For **in-process** types, the Service subclass is loaded via the
    framework's existing loader and introspected for:
      - ``config_class`` → JSON Schema via Pydantic v2
      - ``@service_method``-decorated callables → arg JSON Schemas
      - optional ``state_schema`` + ``topic_schemas`` (published) +
        ``subscribes_schemas`` (consumed/inbound) class attrs
  * For **subprocess** types, the class lives in a per-type venv the
    runtime can't import directly — instead a one-shot script runs
    through THAT venv's own python (see
    ``schema_introspect.subprocess_introspect``), importing the class
    and returning the same ``config_schema``/``state_schema``/
    ``topic_schemas``/``subscribes_schemas``/``methods`` bundle as the
    in-process path. ``schemas_complete`` reflects whether that
    introspection actually succeeded (false only when the venv is
    missing/uninstalled or the class raised on import — see the
    descriptor's ``notes`` field).

Build-once-at-startup: `service_meta` is reconciled at boot and not
hot-reloaded (per ``runtime/catalog.py``). If that ever changes, call
``rebuild()`` to re-introspect + republish.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from botgear.framework.adapters.in_process import _load_service_class
from botgear.runtime.bus import get_bus
from botgear.runtime.identity import get_runtime_id
from rlx_bus.introspect import publishes as class_publishes
from botgear.runtime.schema_introspect import (
    class_state_schema,
    class_topic_schemas,
    class_subscribes_schemas,
    config_class_schema,
    introspect_methods,
    subprocess_introspect,
)


logger = logging.getLogger(__name__)


_SUMMARY_TOPIC = "/runtime/runtime/types"
_PER_TYPE_TOPIC_PREFIX = "/runtime/runtime/types/"


def _resolve_repo_dir() -> Optional[Path]:
    """Mirror lifecycle._resolve_repo_dir without re-importing it (the
    framework adapter does the same dance; we duplicate one helper to
    avoid a circular-import-by-imports-from-imports chain at boot)."""
    try:
        from config import get_settings  # local import: avoid boot-time cycle
        settings = get_settings()
    except Exception:  # noqa: BLE001
        return None
    raw = (getattr(settings, "repo_dir", None) or "repo")
    p = Path(str(raw)).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve()


def _read_service_meta_rows() -> List[Dict[str, Any]]:
    """Read every service_meta row from the DB."""
    try:
        from database.factory import get_database_client
        db = get_database_client()
    except Exception:  # noqa: BLE001
        return []
    if db is None:
        return []
    try:
        return list(db.get_all_items("service_meta") or [])
    except Exception:  # noqa: BLE001
        logger.exception("types_index: failed to read service_meta table")
        return []


def _transport_for(meta: Dict[str, Any]) -> str:
    """Derive transport name from a service_meta row.

    ``language: builtin`` + ``entry_in_process`` → in_process
    ``entry_argv``                              → subprocess
    Anything else falls back to ``unknown``.
    """
    if meta.get("entry_in_process"):
        return "in_process"
    if meta.get("entry_argv"):
        return "subprocess"
    return "unknown"


def _build_descriptor(meta: Dict[str, Any], repo_dir: Optional[Path]) -> Dict[str, Any]:
    """Build one full type descriptor.

    Manifest fields go in straight from the DB row; schema fields only
    populate for in-process types (which we can introspect directly).
    Subprocess types get ``schemas_complete: false`` so consumers know
    to either start an instance + read its meta + filter_catalog, or
    wait for a future phase that ships schemas via the running subprocess.
    """
    type_name = meta.get("name") or "?"
    type_version = meta.get("version") or "1.0.0"
    transport = _transport_for(meta)

    descriptor: Dict[str, Any] = {
        "type": type_name,
        "version": type_version,
        "transport": transport,
        "description": meta.get("description"),
        "tags": list(meta.get("tags") or []),
        "implements": list(meta.get("implements") or []),
        "requires": list(meta.get("requires") or []),
        "author": meta.get("author"),
        "language": meta.get("language"),
        "installed": bool(meta.get("installed")),
        "schemas_complete": False,
        "config_schema": None,
        "state_schema": None,
        "publishes": [],
        "topic_schemas": {},
        "subscribes_schemas": {},
        "methods": [],
        "sub_resources": [],
    }

    if transport != "in_process":
        # Subprocess types get the SAME schema bundle as in-process ones —
        # asking the type's own venv to import its Service class and hand
        # back rlx_bus.introspect.describe_class(cls) as JSON. No running
        # instance required.
        #
        # Hint the canonical instance-side catalog topics so a consumer
        # knows where to look at runtime for things this descriptor still
        # can't cover (e.g. video's per-instance filter catalog).
        if type_name == "video":
            descriptor["sub_resources"] = [{
                "name": "filters",
                "list_topic_suffix": "filters",
                "item_topic_template": "filter/{id}",
                "catalog_topic_suffix": "filter_catalog",
                "key_field": "type",
            }]
        type_root = Path(meta["repo_root"]) if meta.get("repo_root") else repo_dir
        introspected = None
        if type_root is not None:
            from botgear.runtime import proc_compat
            venv_python = proc_compat.venv_python(type_root / type_name / type_version / ".venv")
            argv = list(meta.get("entry_argv") or [])
            introspected = subprocess_introspect(venv_python, type_name, argv)
        if introspected is not None:
            descriptor["config_schema"] = introspected.get("config_schema")
            descriptor["state_schema"] = introspected.get("state_schema")
            descriptor["topic_schemas"] = introspected.get("topic_schemas") or {}
            descriptor["subscribes_schemas"] = introspected.get("subscribes_schemas") or {}
            descriptor["publishes"] = introspected.get("publishes") or []
            descriptor["methods"] = introspected.get("methods") or []
            descriptor["schemas_complete"] = True
        else:
            descriptor["notes"] = (
                "Subprocess type — introspection script failed (venv "
                "missing/not yet installed, or the class raised on "
                "import). Subscribe to a running instance's /meta topic "
                "as a fallback."
            )
        return descriptor

    # ── in-process path: load the class + introspect ──
    entry = meta.get("entry_in_process") or {}
    module_name = entry.get("module") or type_name
    class_name = entry.get("class") or ""
    # Prefer the per-row repo_root recorded by reconcile (a type may live
    # in a read-only repo_paths root, not the writable repo_dir).
    type_root = Path(meta["repo_root"]) if meta.get("repo_root") else repo_dir
    if not type_root or not class_name:
        descriptor["notes"] = "Missing entry_in_process.class in service_meta — cannot introspect."
        return descriptor
    try:
        cls = _load_service_class(type_root, type_name, type_version, module_name, class_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("types_index: failed to load %s/%s/%s: %s",
                       type_name, type_version, class_name, exc)
        descriptor["notes"] = f"Service class load failed: {exc!r}"
        return descriptor

    descriptor["config_schema"] = config_class_schema(cls)
    descriptor["state_schema"] = class_state_schema(cls)
    descriptor["topic_schemas"] = class_topic_schemas(cls)
    descriptor["subscribes_schemas"] = class_subscribes_schemas(cls)
    descriptor["publishes"] = class_publishes(cls)
    descriptor["methods"] = introspect_methods(cls)
    descriptor["schemas_complete"] = True
    return descriptor


def _publish_descriptors(descriptors: Dict[str, Dict[str, Any]]) -> None:
    """Publish the per-type retained topics + the flat summary."""
    bus = get_bus()
    runtime_id = get_runtime_id() or None
    # Per-type
    for type_name, d in descriptors.items():
        bus.publish_sync(_PER_TYPE_TOPIC_PREFIX + type_name, d, retained=True)
    # Summary — keep it small so a consumer can subscribe just to
    # /runtime/runtime/types and learn what types exist without
    # downloading every full schema. Each entry has just the keys a UI
    # would render in a list.
    summary = {
        "ts": time.time(),
        "runtime_id": runtime_id,
        "types": [
            {
                "type": d["type"],
                "version": d["version"],
                "transport": d["transport"],
                "description": d.get("description"),
                "tags": d.get("tags") or [],
                "installed": d.get("installed", False),
                "schemas_complete": d.get("schemas_complete", False),
            }
            for d in sorted(descriptors.values(), key=lambda x: x.get("type") or "")
        ],
    }
    bus.publish_sync(_SUMMARY_TOPIC, summary, retained=True)
    logger.info("runtime.types_index: published %d type descriptors (%d in-process, %d subprocess)",
                len(descriptors),
                sum(1 for d in descriptors.values() if d.get("transport") == "in_process"),
                sum(1 for d in descriptors.values() if d.get("transport") == "subprocess"))


# ─── module-level singleton ──────────────────────────────────────────


_built: Dict[str, Dict[str, Any]] = {}


def _persist_config_schema(meta_row: Dict[str, Any], schema: Dict[str, Any]) -> None:
    """Write an introspected ``config_schema`` back onto its
    ``service_meta`` DB row so the plain REST list (``/v1/service-meta-
    list``, what the UI's catalog + ConfigWizard actually read — they
    don't subscribe to the bus descriptor) serves it too, not just the
    bus descriptor. Skipped when unchanged. Best-effort: a DB hiccup
    here must not break the bus publish it's piggybacking on.

    ``reconcile_catalog``'s per-boot manifest upsert won't clobber this —
    ``update_item`` merges onto the existing row rather than replacing
    it (see AGENTS.md's DB-backend notes), and we round-trip the full
    row anyway.
    """
    if meta_row.get("config_schema") == schema:
        return
    meta_id = meta_row.get("id")
    if not meta_id:
        return
    try:
        from database.factory import get_database_client
        db = get_database_client()
        if db is None:
            return
        row = db.get_item("service_meta", meta_id)
        if not row:
            return
        row["config_schema"] = schema
        db.update_item("service_meta", meta_id, row, include_nulls=True)
    except Exception:  # noqa: BLE001
        logger.exception("types_index: failed to persist config_schema for %s", meta_id)


def rebuild() -> Dict[str, Dict[str, Any]]:
    """Rescan service_meta + republish every descriptor. Returns the
    new descriptors map (keyed by type-name).

    Idempotent — safe to call from on_startup, from a hot-reload hook,
    or from a test.
    """
    global _built
    repo_dir = _resolve_repo_dir()
    rows = _read_service_meta_rows()
    new: Dict[str, Dict[str, Any]] = {}
    for meta in rows:
        type_name = meta.get("name")
        if not type_name or type_name in new:
            continue
        descriptor = _build_descriptor(meta, repo_dir)
        new[type_name] = descriptor
        # Both transports: the UI's Configure button and ConfigWizard read
        # config_schema off the REST service_meta list, not the bus. Built-in
        # types (agent, brain, clock, ...) were left out, so they never got a
        # Configure button at all.
        if descriptor.get("config_schema") is not None:
            _persist_config_schema(meta, descriptor["config_schema"])
    # Clear stale entries: anything in _built but not in new gets its
    # retained topic nulled.
    bus = get_bus()
    for stale in (set(_built) - set(new)):
        bus.publish_sync(_PER_TYPE_TOPIC_PREFIX + stale, None, retained=True)
    _built = new
    _publish_descriptors(_built)
    return _built


def start_publisher() -> None:
    """Build + publish on first call. Idempotent — a second invocation
    re-runs ``rebuild()`` (useful for tests; cheap in production)."""
    rebuild()


def stop_publisher() -> None:
    """Clear retained topics so a fresh subscriber after restart doesn't
    see stale descriptors."""
    global _built
    bus = get_bus()
    try:
        for type_name in _built:
            bus.publish_sync(_PER_TYPE_TOPIC_PREFIX + type_name, None, retained=True)
        bus.publish_sync(_SUMMARY_TOPIC, None, retained=True)
    except Exception:  # noqa: BLE001
        logger.debug("types_index: clear on stop failed (bus may be down)")
    _built = {}
