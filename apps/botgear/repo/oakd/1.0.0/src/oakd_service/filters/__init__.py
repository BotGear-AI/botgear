"""Filter registry + factory — oakd's own copy of video_service's
pattern (see this type's README.md for why this is duplicated rather
than shared: zero risk to the already-working video service; a shared
``rlx_vision`` package is a documented future extraction once there's
proven drift pain across ≥2 real consumers).

To add a new filter type:
  1. Create ``filters/<name>.py`` subclassing ``Filter``
  2. Add it to ``_REGISTRY`` below

The catalog message published on /oakd/<id>/filter_catalog is built
from ``catalog()`` so the UI's Add-filter dropdown stays in sync
without any extra wiring.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from .base import Filter
from .overlay import OverlayFilter
from .yolo import YoloFilter


# v1 starts small (see the video service for the fuller effects/
# detectors set — flip/rotate/canny/motion/etc. weren't ported here;
# most don't apply to a depth-sensing rig the same way). ``yolo`` is
# the one exception: host-CPU YOLO inference on the decoded RGB frame,
# same as the video service's — NOT the on-device spatial-detection
# network (that fuses detection with the stereo depth map for real
# (x, y, z)), which remains a documented future phase, per
# this type's README.md.
_REGISTRY: List[Type[Filter]] = [
    OverlayFilter,
    YoloFilter,
]

_BY_TYPE: Dict[str, Type[Filter]] = {cls.type_name: cls for cls in _REGISTRY}


def catalog() -> List[Dict[str, Any]]:
    """Catalog of every registered filter type. Published once on
    startup as a retained bus message so the UI gets it on first
    subscribe + on every reconnect."""
    return [cls.catalog_entry() for cls in _REGISTRY]


def build_filter(spec: Dict[str, Any]) -> Optional[Filter]:
    """Instantiate a Filter from a persisted spec.

    Spec shape::

        {"id": "uuid", "type": "overlay", "enabled": true,
         "params": {"mode": "timestamp"}}

    Returns None for unknown types or malformed specs — the caller
    skips it so a typo in saved config can't crash the whole pipeline.
    """
    if not isinstance(spec, dict):
        return None
    type_name = spec.get("type")
    if not type_name or type_name not in _BY_TYPE:
        return None
    cls = _BY_TYPE[type_name]
    filter_id = spec.get("id") or f"{type_name}-{id(spec):x}"
    params = spec.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    return cls(id=filter_id, params=params)


__all__ = ["Filter", "catalog", "build_filter"]
