# unmanaged
"""Publish proposed tool calls to the bus + await the reply.

Moved to botgear's shared agent surface (``botgear/agent_surface/invoke.py``,
docs/AGENT_ROADMAP.md Phase 0) so every mind calls services the same way;
re-exported here under brain's historical import path.
"""
from botgear.agent_surface.invoke import ToolExecutor  # noqa: F401
