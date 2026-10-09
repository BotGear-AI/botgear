"""Shared JSON-Schema introspection for service classes.

Used two ways, by the same functions:

  * Directly, when the caller can import the class in-process (botgear's
    ``runtime.schema_introspect`` delegates here for in-process types).
  * Via a one-shot subprocess run through a TYPE's OWN venv (see
    ``runtime.schema_introspect``'s ``_SUBPROCESS_INTROSPECT_SCRIPT``),
    for subprocess types whose class the backend can never import
    directly. This module lives in ``rlx_bus`` specifically because
    it's the one package guaranteed installed in both places (every
    subprocess venv gets it pre-installed — see
    ``installer.py:_rlx_bus_local_path``) — that's what makes a single
    shared implementation possible instead of two that can drift.

Closes ``docs/TODO_BRAIN_GAP_FIXES.md`` Gap 6's deliberately-deferred
item: subprocess methods now get a real signature-derived
``args_schema`` instead of relying only on the manual
``@service_method(args_schema=...)`` stopgap. That override is kept
and takes priority — still useful for signatures this can't resolve
(bare ``**kwargs``, exotic types) — introspection is the fallback, not
a replacement.
"""
from __future__ import annotations

import functools
import inspect
import json
from typing import Any, Dict, List, Optional, Tuple, Type

from pydantic import BaseModel, TypeAdapter

from .methods import _SERVICE_METHOD_ATTR, MethodInfo

__all__ = [
    "config_schema",
    "state_schema",
    "topic_schemas",
    "subscribes_schemas",
    "args_schema_for",
    "methods",
    "publishes",
    "describe_class",
]

# Parameters never relevant to wire callers — dispatcher internals.
_SKIP_PARAMS = frozenset({"self", "cls"})


def config_schema(cls: Type) -> Optional[Dict[str, Any]]:
    """JSON Schema of ``cls.config_class`` (a ``ServiceConfig`` subclass),
    or None if the class declares no ``config_class``."""
    config_class = getattr(cls, "config_class", None)
    if config_class is None:
        return None
    try:
        return config_class.model_json_schema()
    except Exception:  # noqa: BLE001
        return None


def state_schema(cls: Type) -> Optional[Dict[str, Any]]:
    """JSON Schema of ``cls.state_schema`` if declared (a BaseModel
    subclass), else None."""
    schema_cls = getattr(cls, "state_schema", None)
    if not (isinstance(schema_cls, type) and issubclass(schema_cls, BaseModel)):
        return None
    try:
        return schema_cls.model_json_schema()
    except Exception:  # noqa: BLE001
        return None


def _class_attr_schema_map(cls: Type, attr: str) -> Dict[str, Dict[str, Any]]:
    """``{topic_suffix_or_path: JSON Schema}`` built from ``cls.<attr>``,
    a dict whose values are Pydantic ``BaseModel`` subclasses (preferred)
    or pre-built JSON Schema dicts (passed through unchanged). Anything
    else is dropped. Shared by ``topic_schemas`` and
    ``subscribes_schemas`` — same shape, different direction."""
    raw = getattr(cls, attr, None) or {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for name, value in raw.items():
        if isinstance(value, dict):
            out[name] = value
        elif isinstance(value, type) and issubclass(value, BaseModel):
            try:
                out[name] = value.model_json_schema()
            except Exception:  # noqa: BLE001
                pass
    return out


def topic_schemas(cls: Type) -> Dict[str, Dict[str, Any]]:
    """``{topic_suffix: JSON Schema}`` built from ``cls.topic_schemas`` —
    topics this class PUBLISHES."""
    return _class_attr_schema_map(cls, "topic_schemas")


def subscribes_schemas(cls: Type) -> Dict[str, Dict[str, Any]]:
    """``{topic_suffix_or_absolute_path: JSON Schema}`` built from
    ``cls.subscribes_schemas`` — topics this class SUBSCRIBES to
    (inbound). Keys may be a relative suffix (resolved the same way
    ``Service.topic()`` resolves publish suffixes) or an absolute path
    when the class subscribes to another service's topic directly.

    Convention::

        class ChatService(Service):
            subscribes_schemas = {"inbox": InboxPayload}

    Symmetric with ``topic_schemas`` — same value shape, opposite
    direction. Kept as a separate attribute (not folded into
    ``topic_schemas`` with a direction flag) so a service that publishes
    and subscribes to different topics doesn't need to disambiguate
    per-entry.
    """
    return _class_attr_schema_map(cls, "subscribes_schemas")


@functools.lru_cache(maxsize=512)
def _adapt(ann: Any) -> Dict[str, Any]:
    """Cached ``TypeAdapter(ann).json_schema()`` — the same annotation
    reused across many methods shouldn't re-run schema generation."""
    return TypeAdapter(ann).json_schema()


def _param_schema(param: inspect.Parameter) -> Dict[str, Any]:
    schema: Dict[str, Any] = {}
    if param.annotation is not inspect.Parameter.empty:
        try:
            schema = _adapt(param.annotation)
        except Exception:  # noqa: BLE001 — TypeAdapter can raise for exotic types
            schema = {}
    if param.default is not inspect.Parameter.empty:
        try:
            json.dumps(param.default)
            schema["default"] = param.default
        except (TypeError, ValueError):
            pass
    return schema


def args_schema_for(fn: Any) -> Dict[str, Any]:
    """JSON Schema for the arguments ``fn`` (a ``@service_method``
    -decorated callable) accepts, derived from its signature.

    Untyped params get ``{}`` (accept anything); a default makes a
    param non-required. ``*args`` is skipped — wire dispatch goes through
    a dict; ``**kwargs`` makes the schema open (``additionalProperties:
    true``) instead of claiming the method takes nothing else.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {"type": "object", "properties": {}, "additionalProperties": True}
    properties: Dict[str, Any] = {}
    required: List[str] = []
    open_kwargs = False
    for name, param in sig.parameters.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            open_kwargs = True          # **kwargs: it takes more than it names
            continue
        if name in _SKIP_PARAMS or param.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        properties[name] = _param_schema(param)
        if param.default is inspect.Parameter.empty:
            required.append(name)
    out: Dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": open_kwargs}
    if required:
        out["required"] = required
    return out


def methods(cls: Type) -> List[Dict[str, Any]]:
    """Walk the CLASS (not an instance — works cross-venv with no live
    service running) for @service_method-tagged callables.

    ``args_schema`` priority: an explicit
    ``@service_method(args_schema=...)`` override wins (the Gap 6
    stopgap — still useful for signatures this can't resolve);
    otherwise it's derived from the real signature via
    :func:`args_schema_for`.

    Sorted by wire name for deterministic descriptors.
    """
    found: List[Tuple[str, Dict[str, Any]]] = []
    seen: set = set()
    for attr in dir(cls):
        if attr.startswith("_"):
            continue
        try:
            value = getattr(cls, attr)
        except Exception:  # noqa: BLE001
            continue
        info: Optional[MethodInfo] = getattr(value, _SERVICE_METHOD_ATTR, None)
        if info is None or info.name in seen:
            continue
        seen.add(info.name)
        args = info.args_schema if info.args_schema is not None else args_schema_for(value)
        found.append((info.name, {
            "name": info.name,
            "doc": (info.doc or "").strip() or None,
            "args_schema": args,
            "publishes": list(info.publishes or []),
            "publish_return": info.publish_return,
        }))
    found.sort(key=lambda kv: kv[0])
    return [d for _, d in found]


def publishes(cls: Type) -> List[str]:
    """``cls.publishes`` — the class's declared always-on topic suffixes
    (what the Topology page + Composer's Topics tab read). Non-list
    values (e.g. a service that computes it per-instance) yield []."""
    raw = getattr(cls, "publishes", None)
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(t) for t in raw]


def describe_class(cls: Type) -> Dict[str, Any]:
    """Full introspection bundle for one class — the one call both the
    in-process path and the subprocess one-shot script make."""
    return {
        "publishes": publishes(cls),
        "config_schema": config_schema(cls),
        "state_schema": state_schema(cls),
        "topic_schemas": topic_schemas(cls),
        "subscribes_schemas": subscribes_schemas(cls),
        "methods": methods(cls),
    }
