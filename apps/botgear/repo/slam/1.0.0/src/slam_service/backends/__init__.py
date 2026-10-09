# unmanaged
"""Mapping-backend registry. See docs/TODO_SLAM.md §5.2a.

v1 ships exactly one implementation. To add another (e.g. a future
BreezySlamBackend once the ARM/install-wizard question from §5.2a has
an answer): create `backends/<name>.py` subclassing `MappingBackend`,
add it to `_REGISTRY` below. Same small-registry pattern as
video/oakd's `filters/__init__.py`.
"""
from __future__ import annotations

from typing import Dict, Type

from ..config import SlamConfig
from .base import MappingBackend
from .hand_rolled import HandRolledBackend

_REGISTRY: Dict[str, Type[MappingBackend]] = {
    "hand_rolled": HandRolledBackend,
}


def build_backend(config: SlamConfig) -> MappingBackend:
    cls = _REGISTRY.get(config.mapping_backend)
    if cls is None:
        raise ValueError(
            f"unknown mapping_backend {config.mapping_backend!r} — available: {sorted(_REGISTRY)}"
        )
    return cls(config)


__all__ = ["MappingBackend", "HandRolledBackend", "build_backend"]
