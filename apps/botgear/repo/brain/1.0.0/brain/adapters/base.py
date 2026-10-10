# unmanaged
"""ModelAdapter ABC. Adapters return ``ModelResponse`` from
``complete()`` — never raw text; the parsing-into-an-action burden
lives in the adapter, not the workflow engine."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from brain.schemas import ChatMessage, ModelResponse, ToolDescriptor


# Function-calling APIs (Ollama, OpenAI, Anthropic) constrain tool names
# to this shape — no slashes, no ``::``, so the brain's natural tool
# identity (``<topic>::<action>``) can't go on the wire as-is.
_VALID_TOOL_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
_INVALID_NAME_CHARS_RE = re.compile(r"[^a-zA-Z0-9_-]")


def resolve_tool_names(tools: List[ToolDescriptor]) -> List[str]:
    """Compute one provider-safe, legible name per tool, in order.

    Every adapter needs this — duplicated three times with drifting
    behavior before this was factored out (see README.md "Tool names"). Strategy, applied per tool against names already claimed by
    earlier tools in this same list:

    1. The bare action name, if it's a valid tool-name shape AND not
       already claimed. This is the common case (one live instance of a
       given action) and matches what every workflow prompt already says
       ("call `listen`", "call `write_memory`") — untouched by this
       function when it applies.
    2. ``<proxy_id>_<action>`` (sanitized, capped at 64 chars) when the
       bare name collides or isn't a valid shape — e.g. three `tasmota`
       proxies all exposing `power_set` become `office-cord_power_set`,
       `sonoff-cord_power_set`, `sonoff-office-light_power_set` instead
       of the previous opaque, iteration-order-dependent `tool_003`.
       ``proxy_id`` is read off the tool's topic (the segment immediately
       before the topic's last segment, normally ``control``).
    3. ``tool_<index>`` only if even the qualified name is somehow
       already claimed (pathological — shouldn't happen since topics are
       unique per live proxy).
    """
    names: List[str] = []
    used: set = set()
    for i, t in enumerate(tools):
        action = t.action or ""
        candidate = action if _VALID_TOOL_NAME_RE.match(action) and action not in used else ""
        if not candidate:
            candidate = _sanitize_tool_name(f"{_proxy_id_from_topic(t.topic)}_{action}")
            if not candidate or candidate in used:
                candidate = f"tool_{i:03d}"
        used.add(candidate)
        names.append(candidate)
    return names


def _proxy_id_from_topic(topic: str) -> str:
    """``/tasmota/office-cord/control`` -> ``office-cord``. Falls back to
    the last segment if the topic is shorter than expected."""
    parts = [p for p in (topic or "").split("/") if p]
    if len(parts) >= 2:
        return parts[-2]
    return parts[-1] if parts else "svc"


def _sanitize_tool_name(raw: str) -> str:
    cleaned = _INVALID_NAME_CHARS_RE.sub("_", raw)[:64]
    if not cleaned or not cleaned[0].isalpha():
        cleaned = ("t_" + cleaned)[:64]
    return cleaned


class ModelAdapter(ABC):
    """Common interface every model backend implements.

    The workflow engine calls ``await adapter.complete(messages, tools)``
    once per step. Implementations should:
      * Translate ``tools`` into the provider's native tool-calling
        format (OpenAI functions, Anthropic tool_use, Ollama tools).
      * Parse the provider's response into a ``ProposedAction`` — a
        tool call or a terminal ``done``.
      * Fill ``ModelUsage`` if the provider reports it; workflows
        rely on this for ``max_tokens_per_run`` enforcement.
    """

    name: str = "abstract"

    @abstractmethod
    async def complete(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDescriptor]] = None,
        *,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> ModelResponse:
        """Run one chat completion.

        ``model`` overrides the adapter's default model id for this
        single call. Used by workflows that pin ``preferred_model``
        (e.g. ``take_a_note`` pins ``llama3.2:3b``). When None the
        adapter uses its configured ``self.model``.
        """
        ...

    def encode_tools_for_log(
        self, tools: List[ToolDescriptor],
    ) -> List[Dict[str, Any]]:
        """Return the wire-format tools array — what the LLM
        actually receives — for run-folder logging. Default falls
        back to the brain's internal ``ToolDescriptor`` shape;
        provider adapters override to return their native format
        (Ollama / OpenAI ``{type: "function", function: {…}}``,
        Anthropic ``{name, description, input_schema}``).

        Used by ``WorkflowEngine`` when writing ``runs/<id>/tools.json``
        + the ``tools`` field of each ``requests.jsonl`` entry so the
        operator can read exactly what got sent to the provider.
        Adapters that don't override get the bookkeeping shape, which
        is fine — those are stub/test adapters that never reach a
        real API."""
        return [t.model_dump() for t in tools]
