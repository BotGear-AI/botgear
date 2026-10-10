# unmanaged
"""Pydantic models that the rest of the brain reads + writes.

Every cross-module data structure flows through one of these. They're
deliberately small + literal — the brain doesn't have a separate ORM,
DB schema, or wire-format layer. Workflow YAMLs parse into these;
bus payloads serialise from these; run logs are JSON lines of these.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

# Tool shapes + tool policy are botgear's shared agent surface (see
# docs/AGENT_ROADMAP.md, Phase 0) — re-exported here so every brain module
# and test keeps importing them from brain.schemas.
from botgear.agent_surface.models import (  # noqa: F401
    SafetyVerdict,
    ToolCallRecord,
    ToolDescriptor,
    ToolPattern,
    ToolPolicy,
    ToolResult,
)

# brain's historical name for a workflow's tool policy (allowed_tools.yaml).
AllowedTools = ToolPolicy


# ─── workflow definition (parsed from disk) ──────────────────────────


class WorkflowInput(BaseModel):
    """One entry in workflow.yaml ``inputs:`` — per-run argument schema."""
    type: Literal["string", "integer", "boolean", "number"] = "string"
    description: Optional[str] = None
    required: bool = False
    default: Any = None


class WorkflowStep(BaseModel):
    """One step in a workflow's state machine. v1 has a single 'default'
    step that loops; the multi-step graph is just an extension point."""
    id: str = "default"
    prompt: str = "prompt.md"
    on_success: str = "success.md"
    on_failure: str = "failure.md"


class ToolTerminationCondition(BaseModel):
    """Engine-driven terminal condition. Once every action listed in a
    workflow's ``terminate_on`` has been called successfully at least
    once, the engine terminates the run as success WITHOUT requiring
    the model to emit ``done``.

    Matches by action name only — proxy ID and args are not part of
    the match. The workflow's ``allowed_tools`` already constrains
    which proxies are reachable; args matching adds complexity without
    a current use case (revisit if a workflow needs it).
    """
    action: str


class ExitOnPhrase(BaseModel):
    """Engine-enforced exit-phrase interceptor for long-running
    workflows. The engine spawns a watcher task at run start that
    subscribes to ``listen_topic`` and matches every incoming text
    payload against ``matches``. On match, the engine force-
    terminates the run as ``success`` — bypassing the LLM entirely
    so exit latency is bus-RTT (<50ms) not inference-latency.

    Why engine-enforced instead of prompted to the LLM: across long
    conversations, model drift makes "emit done on phrase X" an
    unreliable contract. The engine guarantees it.

    ``whole_message=True`` (default) requires the utterance to equal
    one of the matches exactly (after strip + optional case fold) —
    prevents "stop the servo" from matching a "stop" exit phrase.
    ``whole_message=False`` substring-matches; suitable when the
    operator might prefix/suffix the exit phrase."""
    matches: List[str] = Field(default_factory=list)
    case_insensitive: bool = True
    whole_message: bool = True
    listen_topic: Optional[Union[str, List[str]]] = Field(
        default=None,
        description="Wildcard or exact topic(s) to subscribe to for exit-phrase detection. "
                    "Payload's ``text`` field is the candidate string. "
                    "Example: ``/chat/+/inbox`` to catch every chat instance. Accepts a LIST "
                    "of topics too — needed when more than one `messaging`-capability service "
                    "type might be running (see packages/rlx_messaging/README.md): a single MQTT-style "
                    "pattern can wildcard one path SEGMENT but can't alternate between "
                    "different literal type-name segments, e.g. it can't express "
                    "'/chat/+/inbox OR /discord/+/inbox' as one string.",
    )

    @property
    def listen_topics(self) -> List[str]:
        """Normalized list form of ``listen_topic`` — empty if unset, a
        single-element list for the common string case, or the list
        itself when multiple topics were given."""
        if self.listen_topic is None:
            return []
        if isinstance(self.listen_topic, str):
            return [self.listen_topic]
        return list(self.listen_topic)


class LoopOnTimeout(BaseModel):
    """Fast-path for tools that return a timeout marker (e.g. chat
    service's ``listen`` returns ``{text: '', timeout: True}`` when
    no message arrives). When the engine sees a tool result that
    matches this config, it locally re-dispatches the same tool call
    without going back to the LLM — saving the round trip on every
    idle tick. Bounds (cancel, run timeout, exit phrase) still apply.

    Example for the chat service::

        loop_on_timeout:
          tool: listen
          field: timeout
          value: true"""
    tool: str = Field(..., description="Action name of the tool whose result triggers the re-loop.")
    field: str = Field(..., description="Key on the tool's result dict to inspect.")
    value: Any = Field(..., description="Matched value of that key — when equal, re-dispatch.")


class EchoFilter(BaseModel):
    """Backstop against the robot answering its own voice: a ``listen``
    result that mostly repeats something the robot ``speak``-ed within the
    last ``window_seconds`` is treated as an echo and silently re-listened
    (same fast path as LoopOnTimeout). The primary defence is the STT
    service's echo gate (it stops transcribing while speech plays); this
    catches what that can't see, e.g. a speaker the runtime doesn't control.

    ``threshold`` is the fraction of the heard words that appear, in order,
    in the spoken text. Utterances shorter than ``min_words`` are never
    filtered ("yes", "stop"). An operator who really does repeat the robot
    just says it again: the same text heard twice in a row passes."""
    enabled: bool = True
    window_seconds: float = 15.0
    min_words: int = 3
    threshold: float = 0.75


class LoopBreaker(BaseModel):
    """Automatic circuit breaker for a conversation run that has stopped
    making sense — the model talking to itself instead of the operator.
    Trips (run ends as a failure + its speech is stopped) when either:

      * ``max_consecutive_speaks`` ``speak`` calls succeed with no real
        operator utterance heard in between (the general_assistant loop
        spoke 3-4 times in a row), or
      * the echo filter drops ``max_echo_drops`` utterances within
        ``echo_drop_window_seconds`` (the robot keeps hearing itself).

    0 disables that individual check."""
    enabled: bool = True
    max_consecutive_speaks: int = 4
    max_echo_drops: int = 3
    echo_drop_window_seconds: float = 60.0


class RunConfiguration(BaseModel):
    """One named (backend, model) combo the operator wants to switch
    between quickly. Saved into ``workflow.yaml::configurations``.

    Configurations are operator-curated experimentation aids — the
    workflow's canonical default still lives in ``preferred_backend``
    / ``preferred_model``. The toolbar's configuration dropdown lets
    the operator flick between saved combos without editing yaml;
    selecting one populates the backend + model fields, and the Run
    button passes those values into ``start_workflow(backend=,
    model=)``.

    Names must be unique within a workflow's ``configurations`` list.
    They're free-form labels — operators use them as "fast", "smart",
    "production", etc. ``description`` is optional rationale that
    surfaces as a tooltip in the dropdown."""
    name: str = Field(..., description="Unique label within this workflow's configurations.")
    backend: str = Field(..., description="Adapter name — one of mock / ollama / anthropic / openai.")
    model: Optional[str] = Field(default=None, description="Model id (e.g. llama3.2:3b). None = adapter default.")
    description: str = ""


class Workflow(BaseModel):
    """Parsed workflow.yaml + sibling files. Created by context_loader."""
    name: str
    description: str = ""
    # Which BACKEND (adapter) the workflow prefers — one of
    # ``mock`` / ``ollama`` / ``anthropic`` / ``openai``. The brain
    # falls back to ``BrainConfig.default_model`` (also a backend
    # name, despite the legacy field name) when this isn't set, and a
    # per-call ``start_workflow(backend=...)`` arg overrides both.
    # Previously named ``model`` — that label was misleading because
    # this never named a specific LLM model; it picked the adapter
    # only. The model id used at runtime is configured per-backend on
    # ``BrainConfig.<backend>_model`` (e.g. ``ollama_model``) and can
    # be pinned per-workflow via ``preferred_model`` below.
    preferred_backend: str = "mock"
    # Optional pin on the model id (e.g. ``llama3.2:3b``,
    # ``claude-opus-4-8``). When set, this overrides the backend's
    # default model just for this workflow's runs. Useful for
    # workflows whose tool-call behaviour is sensitive to model
    # quirks — ``take_a_note`` pins ``llama3.2:3b`` because
    # ``llama3.1:8b`` fails its schema interpretation on the
    # ``write_memory`` arg shape. See TESTING_RESULTS.md.
    preferred_model: Optional[str] = None
    # Operator-curated alternates to ``preferred_*``. Surfaced via the
    # brain panel's configuration dropdown so the operator can flick
    # between saved (backend, model) combos without editing yaml. Read
    # by the brain via ``start_workflow(configuration=<name>)`` — the
    # engine looks up the entry and substitutes its backend + model
    # before the run starts.
    configurations: List[RunConfiguration] = Field(default_factory=list)
    max_steps: int = 8
    timeout_seconds: int = 120
    max_tokens_per_run: Optional[int] = None
    requires_human_approval: bool = False
    # Progressive tool disclosure. Off by default — zero behavior
    # change for every existing workflow. When True, the model isn't
    # shown full schemas for every allowed action up front; only for
    # whatever's in `always_loaded_prefixes` (plus the implicit
    # "brain" prefix, for describe_capability itself + brain's own
    # core actions) and whatever it has asked `describe_capability`
    # about so far THIS run. Everything else is summarized as a
    # compact index in context instead of a full tool definition.
    # Exists because the full live catalog's token cost scales with
    # how many services happen to be running, which crowds out
    # conversation history fastest in exactly the workflows (broad,
    # default_policy: allow ones) that most want headroom for it. See
    # WorkflowEngine._lazy_tool_descriptors / _capability_index_text.
    lazy_tools: bool = False
    # Capability/topic-prefixes (see safety_gate.capability_prefix)
    # whose actions are ALWAYS fully loaded regardless of discovery
    # state — typically the conversation-I/O capability this
    # workflow's own listen/speak loop structurally depends on every
    # turn (e.g. ["stt", "speech"]). Only meaningful when lazy_tools
    # is True; ignored otherwise.
    always_loaded_prefixes: List[str] = Field(default_factory=list)
    # lazy_tools only kicks in when the allowed live catalog has MORE
    # than this many actions (~90 actions ≈ 6k tokens of schema) —
    # below it, everything is loaded up front: no describe_capability
    # round trips, nothing for the model to wrongly claim it "doesn't
    # have". 0 = always lazy.
    lazy_tools_threshold: int = 120
    inputs: Dict[str, WorkflowInput] = Field(default_factory=dict)
    steps: List[WorkflowStep] = Field(default_factory=lambda: [WorkflowStep()])
    context: List[str] = Field(
        default_factory=list,
        description="Paths to memory files to pull into model context at start.",
    )
    allowed_tools: AllowedTools = Field(default_factory=AllowedTools)
    # Engine-driven termination — see ToolTerminationCondition. When
    # non-empty, the engine terminates as success as soon as ALL listed
    # actions have been called successfully at least once in this run.
    # Empty (default) preserves the legacy done-from-model behavior.
    terminate_on: List[ToolTerminationCondition] = Field(default_factory=list)
    # Engine-enforced exit-phrase interceptor. See ExitOnPhrase.
    # When set, the engine spawns a watcher task at run start that
    # subscribes to the topic, matches incoming text against the
    # patterns, and force-terminates the run on match. Long-running
    # workflows (conversation_servo, conversation_session) use this
    # so the operator can always escape regardless of model drift.
    exit_on_phrase: Optional[ExitOnPhrase] = None
    # Fast-path for tools that return a timeout marker (e.g. chat's
    # ``listen``). On match, engine re-dispatches the tool without an
    # LLM round trip. See LoopOnTimeout.
    loop_on_timeout: Optional[LoopOnTimeout] = None
    # Turn-taking: the engine adds ``wait: true`` to every ``speak`` on a
    # /speech/*/control topic (unless the call sets ``wait`` itself), so the
    # next ``listen`` can't start while the reply is still playing.
    speak_wait: bool = True
    # Self-echo backstop for ``listen`` results. See EchoFilter.
    echo_filter: EchoFilter = Field(default_factory=EchoFilter)
    # Phrase-terminated (exit_on_phrase) conversations: when the model
    # emits ``done`` mid-conversation, speak its text if it wasn't already
    # spoken (small models often put the reply there instead of calling
    # speak) and go straight back to listening — same run, same context —
    # instead of ending. Ignored for workflows without exit_on_phrase.
    resume_on_done: bool = True
    # Append the current local time ("[now: Saturday 2026-10-03 14:05
    # PDT]") to every real heard ``listen`` result, so a long-running
    # conversation can answer "what time is it" — the Environment
    # section's run-start time goes stale. A few tokens per turn.
    stamp_time: bool = True
    # System One reflex bypass (brain/reflex_engine.py): before each model
    # turn, a direct light/switch or pan-tilt command in the latest
    # utterance is executed without the model. Off by default — it
    # actuates hardware on heuristics, so a workflow opts in.
    fast_reflex: bool = False
    # Runaway-conversation circuit breaker. See LoopBreaker.
    loop_breaker: LoopBreaker = Field(default_factory=LoopBreaker)
    # Sliding conversation window. When set, the engine trims the
    # messages array after each turn to keep only the system message
    # + the last N entries. Bounds token usage on long-running
    # workflows without losing the system prompt or recent context.
    # ``None`` (default) keeps the full history.
    message_window_size: Optional[int] = None
    prompt_md: str = ""
    success_md: str = ""
    failure_md: str = ""


# ─── chat / model interface ─────────────────────────────────────────


class ChatMessage(BaseModel):
    """OpenAI-shaped chat message — every modern adapter normalises here."""
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    # Optional fields for tool-message threads.
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    # When the engine appends an assistant message AFTER a tool call,
    # adapters that need the full tool-call shape (anthropic's
    # ``tool_use`` block, openai's ``tool_calls`` array) reconstruct
    # it from ``tool_call_id`` + ``name`` + ``tool_args``. Required for
    # anthropic to thread the follow-up turn's ``tool_result`` to its
    # originating ``tool_use_id``; ignored by adapters that don't care.
    tool_args: Optional[Dict[str, Any]] = None


class ProposedAction(BaseModel):
    """The model's output, parsed into something the brain can validate.

    Either a tool call or a terminal ``done`` signal. Adapters return
    one of these from .complete() — never raw text. If the model
    rambles instead of using a tool, MockAdapter (and real adapters
    via the no-tool-call path) emit ``ProposedAction(kind="done")``
    with the rambling text as ``rationale``.
    """
    kind: Literal["tool", "done"]
    topic: Optional[str] = None
    action: Optional[str] = None
    args: Dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(default="", description="The model's explanation / chain-of-thought.")
    tool_call_id: Optional[str] = None  # round-trips through the chat history


class ModelUsage(BaseModel):
    """Token accounting per ``complete()`` call. Adapters fill what they know."""
    input_tokens: int = 0
    output_tokens: int = 0


class ModelResponse(BaseModel):
    """Everything an adapter returns from .complete().

    ``actions`` holds every proposed action from this one model
    round-trip, in the order the provider returned them — a model can
    (and, observed in practice, sometimes does) emit several tool calls
    in a single response, e.g. an action plus its own confirmation
    ``speak()`` (see README.md "Several tool calls in one response"). Always has at
    least one entry: an adapter that only constructs ``action`` (single-
    action response, or any path written before this field existed)
    gets ``actions=[action]`` for free via the validator below.
    ``action`` is ``actions[0]`` and stays a real field, not just a
    property, for backward compatibility with any code that reads it
    directly."""
    action: ProposedAction
    actions: List[ProposedAction] = Field(default_factory=list)
    raw: Dict[str, Any] = Field(default_factory=dict, description="provider-specific response")
    usage: ModelUsage = Field(default_factory=ModelUsage)

    @model_validator(mode="after")
    def _default_actions_to_action(self) -> "ModelResponse":
        if not self.actions:
            self.actions = [self.action]
        return self


# ─── safety + execution ─────────────────────────────────────────────


# ─── run record (the thing logged to disk + bus) ─────────────────────


class StepRecord(BaseModel):
    ts: str  # iso-8601 UTC
    step: int
    model: str
    prompt_summary: str
    response_raw: Dict[str, Any] = Field(default_factory=dict)
    action: ProposedAction
    verdict: SafetyVerdict
    tool_call_id: Optional[str] = None
    usage: ModelUsage = Field(default_factory=ModelUsage)


class RunRecord(BaseModel):
    """Top-level summary of a single workflow run."""
    run_id: str
    workflow: str
    started_at: str
    ended_at: Optional[str] = None
    status: Literal["pending", "running", "awaiting_approval", "success", "failure", "cancelled"] = "pending"
    inputs: Dict[str, Any] = Field(default_factory=dict)
    # The ADAPTER (provider) the run is using. One of ``mock`` /
    # ``ollama`` / ``anthropic`` / ``openai``. Previously the field was
    # called ``model`` which was misleading — it never named a specific
    # LLM model, only the adapter. Renamed 2026-06-04; no alias.
    backend: str = "mock"
    # The actual LLM model id (e.g. ``llama3.2:3b``, ``claude-opus-4-8``).
    # None means "use the adapter's configured default
    # (BrainConfig.<backend>_model)". Set when the workflow's
    # ``preferred_model`` is non-null or when the operator overrode at
    # start time via ``start_workflow(model=...)``.
    model_id: Optional[str] = None
    steps_used: int = 0
    tokens_used: int = 0
    # Provider-reported spend (session minds report it; None = not known).
    cost_usd: Optional[float] = None
    result_summary: str = ""
    failure_reason: Optional[str] = None
