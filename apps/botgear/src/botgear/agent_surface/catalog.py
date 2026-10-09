# unmanaged
"""The live tool catalog: every callable service action on the bus.

A catalog is a plain ``Dict[str, ToolDescriptor]`` keyed by
``<control topic>::<action>``. It is fed by two bus streams:

* ``/+/+/meta`` (retained) — each service's self-description: control
  topic + ``@service_method``s with docstring and signature-derived
  ``args_schema``. Every method becomes a ToolDescriptor.
* ``/service_proxy/+/lifecycle`` — ``status=uninstalled`` (a release, or a
  rename away) evicts that proxy's tools; meta only ever adds, so without
  this a gone service leaves ghost tools that pass the "live" check and
  time out when called.

The watchers take the bus as a parameter so each consumer (brain, the MCP
gateway) can pass its own — and tests a fake.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from botgear.agent_surface.models import ToolDescriptor

logger = logging.getLogger(__name__)

Catalog = Dict[str, ToolDescriptor]
OnChange = Callable[[], Awaitable[None]]


def control_topic_of(meta: Dict[str, Any]) -> Optional[str]:
    """The control topic a meta payload advertises. ``topics.control`` is
    published fully resolved (topic_remap applied); older subprocess
    services only send ``topics_root``."""
    topics = meta.get("topics") or {}
    control = topics.get("control")
    if isinstance(control, str) and control:
        return control
    root = meta.get("topics_root") or meta.get("topic_root") or ""
    return f"{root}/control" if root else None


def descriptors_from_meta(meta: Any) -> List[ToolDescriptor]:
    """One ToolDescriptor per method in a ``/{type}/{id}/meta`` payload."""
    if not isinstance(meta, dict):
        return []
    control = control_topic_of(meta)
    if not control:
        return []
    out = []
    for m in meta.get("methods") or []:
        name = m.get("name") if isinstance(m, dict) else None
        if not name:
            continue
        out.append(ToolDescriptor(
            topic=control,
            action=name,
            description=(m.get("doc") or "").strip(),
            parameters=m.get("args_schema") or {},
        ))
    return out


def add_meta(catalog: Catalog, meta: Any) -> int:
    """Upsert a meta payload's tools into ``catalog``; returns how many."""
    descriptors = descriptors_from_meta(meta)
    for d in descriptors:
        catalog[d.name] = d
    return len(descriptors)


def evict_proxy(catalog: Catalog, proxy_id: str, type_name: Optional[str] = None) -> int:
    """Drop every entry whose control topic belongs to ``proxy_id``
    (``/<type>/<proxy_id>/…``), optionally only of ``type_name``."""
    doomed = []
    for key, desc in catalog.items():
        parts = (desc.topic or "").split("/")
        if len(parts) > 2 and parts[2] == proxy_id and (type_name is None or parts[1] == type_name):
            doomed.append(key)
    for key in doomed:
        catalog.pop(key, None)
    return len(doomed)


def released_proxy(payload: Any) -> Optional[Tuple[str, Optional[str]]]:
    """``(proxy_id, type_name)`` if a lifecycle payload says the proxy is
    gone (``status=uninstalled``), else None."""
    if not isinstance(payload, dict) or payload.get("status") != "uninstalled":
        return None
    proxy_id = payload.get("id")
    if not isinstance(proxy_id, str) or not proxy_id:
        return None
    type_name = str(payload.get("service_meta_id") or "").split("@")[0] or None
    return proxy_id, type_name


def _payload(msg: Any) -> Any:
    return msg.payload if hasattr(msg, "payload") else msg


async def watch_meta(catalog: Catalog, bus: Any, subscriber_id: str, on_change: OnChange) -> None:
    """Keep ``catalog`` fed from ``/+/+/meta`` until cancelled. Calls
    ``on_change`` after every meta message (even one that added nothing —
    same as brain always did)."""
    try:
        async for msg in bus.subscribe("/+/+/meta", subscriber_id=subscriber_id):
            meta = _payload(msg)
            if not isinstance(meta, dict):
                continue
            add_meta(catalog, meta)
            await on_change()
    except asyncio.CancelledError:
        return
    except Exception:  # noqa: BLE001
        logger.exception("agent_surface: meta watcher crashed; tool discovery paused")


async def watch_releases(
    catalog: Catalog,
    bus: Any,
    subscriber_id: str,
    on_change: OnChange,
    evict: Optional[Callable[[str, Optional[str]], int]] = None,
) -> None:
    """Evict a service's tools when it's released or renamed away, until
    cancelled. ``evict`` overrides how (default: ``evict_proxy`` on
    ``catalog``); ``on_change`` runs only when something was evicted."""
    evict = evict or (lambda pid, t=None: evict_proxy(catalog, pid, t))
    try:
        async for msg in bus.subscribe("/service_proxy/+/lifecycle", subscriber_id=subscriber_id):
            gone = released_proxy(_payload(msg))
            if gone and evict(*gone):
                await on_change()
    except asyncio.CancelledError:
        return
    except Exception:  # noqa: BLE001
        logger.exception("agent_surface: release watcher crashed; stale tools will linger until restart")
