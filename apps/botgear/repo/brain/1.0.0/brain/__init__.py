# unmanaged
"""brain service — folder-defined, AI-agnostic workflow brain.

See this type's ``README.md`` for the full spec. Public entry point is
``BrainService``; everything else under this package is implementation
detail.
"""
from brain.service import BrainService

__all__ = ["BrainService"]
