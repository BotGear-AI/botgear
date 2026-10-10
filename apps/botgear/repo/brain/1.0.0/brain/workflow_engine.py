# unmanaged
"""WorkflowEngine — the step loop.

Owns one ``WorkflowRun`` (the live state of a single run) and walks
through the standard cycle until success / failure / cancel.

Per step:
  1. Build context (workflow.prompt_md + memory + previous tool results).
  2. Build chat messages from the context.
  3. Ask the adapter to ``complete()`` with the allowed-tool descriptors.
  4. Parse the response → ProposedAction.
  5. Validate via the safety_gate.
  6. If ``done`` → write success, terminate.
  7. If blocked → write failure, terminate.
  8. Else publish to the bus via ToolExecutor + record the result.
  9. Loop.

The engine doesn't know about FastAPI, bus internals, or model
vendors. Everything goes through small interfaces (ModelAdapter,
ToolExecutor, RunLogger) — tests can stub each.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import math
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from brain import memory as memory_mod
from brain.adapters.base import ModelAdapter
from brain.context_loader import render_context
from brain.run_logger import RunLogger, _now_iso, make_run_dir
from brain.safety_gate import (
    capability_prefix,
    check_max_steps,
    check_max_tokens,
    check_tool_call,
)
from brain.schemas import (
    ChatMessage,
    ExitOnPhrase,
    ModelResponse,
    ModelUsage,
    ProposedAction,
    RunRecord,
    SafetyVerdict,
    StepRecord,
    ToolCallRecord,
    ToolDescriptor,
    ToolResult,
    Workflow,
)
from brain.reflex_engine import FastReflexEngine
from brain.tool_executor import ToolExecutor
from botgear.runtime.bus import get_bus


logger = logging.getLogger(__name__)


# Callable that performs a tool call. Wraps ToolExecutor.call so the
# engine can be unit-tested by injecting a stub.
ToolCaller = Callable[[str, str, Dict[str, Any]], Awaitable[ToolResult]]


class WorkflowEngine:
    """Run a workflow to completion.

    Single-shot per run — construct, ``await run()``, read the
    terminal ``RunRecord``. Cancel + approve are exposed via the
    optional events injected at construction time.
    """

    def __init__(
        self,
        *,
        workflow: Workflow,
        workspace_dir: Path,
        adapter: ModelAdapter,
        tool_caller: ToolCaller,
        publish: Callable[..., None],
        bus_prefix: str,
        tool_catalog: Optional[Dict[str, ToolDescriptor]] = None,
        inputs: Optional[Dict[str, Any]] = None,
        backend: Optional[str] = None,
        model_id: Optional[str] = None,
        discovered_prefixes: Optional[Any] = None,
        runtime_facts: Optional[Callable[[], Dict[str, Any]]] = None,
        on_change: Optional[Callable[[], None]] = None,
    ):
        self.workflow = workflow
        self.workspace_dir = workspace_dir
        self.adapter = adapter
        self.tool_caller = tool_caller
        self.tool_catalog = tool_catalog or {}
        self.inputs = inputs or {}
        # Direct publish callable (the RunLogger has a separate one
        # scoped to /runs/<run_id>); workflow_events fire on
        # /brain/<proxy_id>/workflow_events so we keep this raw.
        self._publish = publish
        # Per-call model pin. ``model_id`` overrides the adapter's
        # configured default on every ``complete()`` call (see
        # ``_loop``). ``backend`` is the adapter's name — passed in
        # rather than read off ``self.adapter.name`` so the RunRecord
        # matches whatever the caller resolved (workflow.preferred_*
        # vs config default vs start-time override).
        # Fall back to ``workflow.preferred_model`` when no explicit
        # override was passed in — keeps direct WorkflowEngine
        # callers (mostly tests) working without having to resolve
        # the precedence themselves.
        self._model_id = model_id if model_id is not None else workflow.preferred_model
        # Supplier of what the runtime knows about itself (version, host,
        # battery, ...) for the context's Environment section. None (tests,
        # direct callers) → the section carries just the clock.
        self._runtime_facts = runtime_facts

        run_id = uuid.uuid4().hex[:8]
        self.run_id = run_id
        self.run_dir = make_run_dir(workspace_dir, workflow.name, run_id)
        self.logger = RunLogger(
            run_dir=self.run_dir,
            publish=publish,
            bus_prefix=f"{bus_prefix.rstrip('/')}/runs/{run_id}",
        )
        self.record = RunRecord(
            run_id=run_id,
            workflow=workflow.name,
            started_at=_now_iso(),
            status="pending",
            inputs=self.inputs,
            backend=backend or workflow.preferred_backend,
            model_id=model_id,
        )
        # bus_prefix shape is "/brain/<proxy_id>" — derive proxy_id for
        # the workflow_events topic. Falls back to the run dir's parent
        # workspace path if the prefix is malformed.
        parts = bus_prefix.strip("/").split("/")
        self._proxy_id = parts[1] if len(parts) >= 2 and parts[0] == "brain" else ""
        # Engine-driven termination state. ``_actions_succeeded`` is
        # the set of action names that have been called with
        # result.status == "ok" during this run. Compared against
        # ``workflow.terminate_on`` after each tool call.
        self._actions_succeeded: set = set()
        self._tool_calls_attempted = 0
        self._tool_calls_succeeded = 0
        # Self-echo backstop (workflow.echo_filter): what the robot said
        # recently, as (monotonic time speak returned, spoken text), and the
        # last heard text dropped as an echo (heard again → let through).
        self._recent_spoken: List[tuple] = []
        self._last_echo_dropped: Optional[List[str]] = None
        # Loop breaker (workflow.loop_breaker) + end-of-run silencing:
        # speech control topics this run spoke on, speaks since the
        # operator was last really heard, and when echoes were dropped.
        self._speech_topics: set = set()
        self._speaks_since_heard = 0
        self._echo_drop_times: List[float] = []

        # Approval flow: when requires_human_approval is set, the engine
        # parks before each tool call and waits on this event.
        self._approval_event: Optional[asyncio.Event] = None
        self._approval_decision: Optional[bool] = None
        self._cancel_event = asyncio.Event()

        # Progressive tool disclosure (workflow.lazy_tools). Capability
        # prefixes the model has successfully called describe_capability
        # on so far THIS run — grows over the run, never shrinks. Empty
        # for non-lazy workflows; harmless either way since
        # _lazy_tool_descriptors short-circuits to the full list when
        # workflow.lazy_tools is False.
        # Seeded from earlier runs of the same workflow (BrainService
        # remembers them), so a restarted conversation doesn't have to
        # re-discover the switch it used a minute ago.
        self._discovered_prefixes: set = set(discovered_prefixes or ())
        # Conversation plumbing for resume_on_done / the refusal check:
        # where this run last spoke + listened, and whether the model was
        # already told to check hidden capabilities for this utterance.
        # Live "what is the run doing right now" for the UI — see
        # _set_phase. ``on_change`` (BrainService republishes its state)
        # fires on every phase change.
        self._on_change = on_change
        self.phase: str = "starting"
        self.phase_detail: Optional[str] = None
        self.phase_since: str = _now_iso()
        self.last_heard: Optional[str] = None
        self.last_heard_at: Optional[str] = None
        self.silent_listens = 0
        self._last_speak_topic: Optional[str] = None
        self._last_listen: Optional[tuple] = None        # (topic, args)
        self._refusal_nudged = False
        # Fast System One reflex engine (sub-150ms bypass for direct commands)
        self._reflex_engine = FastReflexEngine()
        self._last_reflex_heard_at: Optional[str] = None
        # Context message (messages[1]) + its parts, so the capability
        # index in it can be rewritten as capabilities get discovered.
        self._context_head = ""
        self._context_index = ""
        self._context_msg: Optional[ChatMessage] = None

    # ─── public lifecycle ──────────────────────────────────────────

    async def run(self) -> RunRecord:
        """Drive the workflow to a terminal state. Always returns the
        final RunRecord; never raises for normal failures (they're
        recorded on the record)."""
        self.logger.write_input({"workflow": self.workflow.name, "inputs": self.inputs})
        # Prepend a small run-header to the LLM context so:
        #   1. The model has self-awareness of which backend + model
        #      is running it (sometimes useful for prompts that branch
        #      on capability, e.g. "if you are a smaller model, keep
        #      replies short").
        #   2. The captured ``runs/<id>/context.md`` artifact is
        #      self-describing — opening the file later tells you
        #      what model produced these outputs without cross-
        #      referencing ``summary.json``.
        header_lines = [
            "# Run context",
            f"- run_id: {self.run_id}",
            f"- workflow: {self.workflow.name}",
            f"- backend: {self.record.backend}",
            f"- model_id: {self.record.model_id or '(adapter default)'}",
            "",
            *self._environment_lines(),
            "---",
            "",
        ]
        self._context_head = "\n".join(header_lines) + render_context(
            self.workflow, self.workspace_dir, self.inputs,
        )
        self._context_index = self._capability_index_text()
        context_body = self._context_message_text()
        self.logger.write_context(context_body)
        self.record.status = "running"
        self._set_phase("starting")
        self.logger.write_summary(self.record)
        # Workflow-level lifecycle event. Operators/UI subscribe to
        # /brain/<proxy_id>/workflow_events to track every workflow
        # run from start to end without parsing per-run topics.
        self._emit_workflow_event("started")

        messages: List[ChatMessage] = [
            ChatMessage(role="system", content=_system_prompt(self.workflow)),
            ChatMessage(role="user", content=context_body),
        ]
        self._context_msg = messages[1]
        session = bool(getattr(self.adapter, "is_session", False))
        # A session mind (claude_agent) gets every allowed tool: lazy
        # disclosure exists for small local models' context windows.
        tools: List[ToolDescriptor] = (
            self._allowed_tool_descriptors() if session else self._lazy_tool_descriptors()
        )
        if session:
            self._context_index = ""
            context_body = self._context_message_text()
            self.logger.write_context(context_body)
        # How many tools the model was actually offered this run. Zero
        # is a common silent failure: the workflow allows e.g. listen/
        # speak but the conversation service isn't running, so the
        # catalog has nothing to offer and the model narrates its calls
        # as text (which parses as ``done``). The done-handler surfaces
        # this in its failure message.
        self._tools_offered_count = len(tools)
        # Capture the tool catalog the LLM will see for this run.
        # Adapters override ``encode_tools_for_log`` to return the
        # provider-native wire shape (Ollama/OpenAI {type:"function",
        # function:{name,description,parameters}}; Anthropic
        # {name,description,input_schema}) — exactly what gets sent
        # on the network. Falls back to the brain's internal
        # ``ToolDescriptor`` shape for stub/test adapters that don't
        # override. Static for the run; written once.
        self.logger.write_tools(self.adapter.encode_tools_for_log(tools))

        # Capture our own task ref so the exit-phrase watcher (spawned
        # below) can cancel us cleanly on match. ``asyncio.current_task()``
        # is the task created in service.py via ``asyncio.create_task(
        # _run_to_completion(engine))``.
        self._run_task = asyncio.current_task()
        self._exit_via_phrase = False
        self._exit_phrase_matched: Optional[str] = None
        watcher: Optional[asyncio.Task] = None
        if self.workflow.exit_on_phrase and self.workflow.exit_on_phrase.listen_topics:
            watcher = asyncio.create_task(self._exit_phrase_watcher())

        try:
            if session:
                await self._session_loop(context_body, tools)
            else:
                await self._loop(messages, tools)
        except asyncio.CancelledError:
            # Distinguish operator-cancel from exit-phrase-cancel. Both
            # raise CancelledError through the await chain; the
            # ``_exit_via_phrase`` flag tells us which initiated it.
            if self._exit_via_phrase:
                self.record.status = "success"
                self.record.result_summary = (
                    f"exit phrase matched: {self._exit_phrase_matched!r}"
                )
                self.record.ended_at = _now_iso()
                self.logger.write_result(self.record.result_summary, status="success")
            else:
                self.record.status = "cancelled"
                if not (self.record.failure_reason or "").startswith("halted"):
                    self.record.failure_reason = "cancelled by operator"
                self.record.ended_at = _now_iso()
                self.logger.write_result(self.record.failure_reason, status="cancelled")
        except Exception as exc:  # noqa: BLE001
            self.record.status = "failure"
            self.record.failure_reason = f"engine error: {type(exc).__name__}: {exc}"
            self.record.ended_at = _now_iso()
            self.logger.log_error(self.record.failure_reason)
            self.logger.write_result(self.record.failure_reason, status="failure")
            logger.exception("brain.workflow_engine: %s/%s crashed", self.workflow.name, self.run_id)
        finally:
            # Make sure the watcher doesn't outlive the run. The watcher
            # exits naturally when its get_bus().subscribe iterator is
            # cancelled.
            if watcher is not None and not watcher.done():
                watcher.cancel()
                try:
                    await watcher
                except BaseException:  # noqa: BLE001
                    pass
            self._silence_speech()
            self.logger.write_summary(self.record)
            self._emit_workflow_event("ended")

        return self.record

    # ─── self-echo backstop ─────────────────────────────────────────

    def _note_spoken(self, action: ProposedAction, result: ToolResult) -> None:
        """Remember what a successful ``speak`` said (the service's cleaned
        text when it reports one) for ``_is_self_echo``, and count it for
        the loop breaker."""
        if action.action != "speak" or result.status != "ok":
            return
        if _SPEECH_CONTROL_RE.match(action.topic or ""):
            self._speech_topics.add(action.topic)
        self._speaks_since_heard += 1
        value = result.value if isinstance(result.value, dict) else {}
        text = value.get("spoken") or (action.args or {}).get("text")
        if isinstance(text, str) and text.strip():
            self._recent_spoken.append((time.monotonic(), text))
            self._recent_spoken = self._recent_spoken[-8:]

    def _deadline(self) -> float:
        """Wall-clock end of the run; ``timeout_seconds <= 0`` means none."""
        if self.workflow.timeout_seconds <= 0:
            return math.inf
        return time.monotonic() + self.workflow.timeout_seconds

    def _is_self_echo(self, action: ProposedAction, result: ToolResult) -> bool:
        """True when a ``listen`` result is (mostly) the robot's own recent
        speech coming back through the microphone. See EchoFilter."""
        cfg = self.workflow.echo_filter
        if not cfg.enabled or action.action != "listen" or result.status != "ok":
            return False
        value = result.value if isinstance(result.value, dict) else {}
        heard = _echo_words(value.get("text") or "")
        if len(heard) < cfg.min_words:
            return False
        if self._last_echo_dropped is not None and _echo_containment(heard, self._last_echo_dropped) >= cfg.threshold:
            # Heard the same thing twice running — the robot didn't speak in
            # between, so this is the operator deliberately repeating it.
            self._last_echo_dropped = None
            return False
        now = time.monotonic()
        for ts, text in reversed(self._recent_spoken):
            if now - ts > cfg.window_seconds:
                break
            score = _echo_containment(heard, _echo_words(text))
            if score >= cfg.threshold:
                self._last_echo_dropped = heard
                self._echo_drop_times.append(now)
                logger.info(
                    "brain run %s: dropped self-echo (%.2f match) %r", self.run_id, score, value.get("text"),
                )
                return True
        self._last_echo_dropped = None
        return False

    # ─── live phase (for the UI) ─────────────────────────────────

    def _set_phase(self, phase: str, detail: Optional[str] = None) -> None:
        """Record what the run is doing right now — ``starting``,
        ``thinking`` (waiting on the model), ``listening``, ``speaking``,
        ``calling`` (any other tool; ``detail`` = action name) — and tell
        the owner so it can republish. Silent re-listens bump
        ``silent_listens`` instead of looking like nothing happened."""
        if phase != self.phase or detail != self.phase_detail:
            self.phase = phase
            self.phase_detail = detail
            self.phase_since = _now_iso()
        self._notify()

    def _notify(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:  # noqa: BLE001 — a UI refresh must never break a run
            logger.debug("brain run %s: on_change failed", self.run_id, exc_info=True)

    def live_summary(self) -> Dict[str, Any]:
        """The fields BrainService puts on this run's active-run card."""
        return {
            "phase": self.phase,
            "phase_detail": self.phase_detail,
            "phase_since": self.phase_since,
            "last_heard": self.last_heard,
            "last_heard_at": self.last_heard_at,
            "silent_listens": self.silent_listens,
            "steps_used": self.record.steps_used,
            "tool_calls": self._tool_calls_attempted,
            "started_at": self.record.started_at,
        }

    def _note_conversation_io(self, action: ProposedAction, result: ToolResult) -> None:
        if result.status != "ok":
            return
        if action.action == "speak":
            self._last_speak_topic = action.topic
        elif action.action == "listen":
            self._last_listen = (action.topic or "", dict(action.args or {}))

    def _premature_refusal(self, action: ProposedAction) -> Optional[str]:
        """If the model is about to SAY it can't do something while live
        capabilities are still unloaded, return the corrective message to
        hand back instead of speaking (once per operator utterance)."""
        if action.action != "speak" or self._refusal_nudged:
            return None
        text = str((action.args or {}).get("text") or "")
        if not _REFUSAL_RE.search(text):
            return None
        hidden = self._hidden_capabilities()
        if not hidden:
            return None
        self._refusal_nudged = True
        logger.info("brain run %s: held back a refusal (%r) — unloaded: %s", self.run_id, text[:80], hidden)
        return (
            "NOT SPOKEN. Before telling the operator you can't do something, check the "
            "live capabilities you haven't loaded yet: " + ", ".join(hidden) + ". Call "
            "describe_capability(capability=<name>) for the one that could do it, then "
            "act. Only say you can't if none of them can."
        )

    async def _synth_call(
        self, topic: str, action_name: str, args: Dict[str, Any], messages: List[ChatMessage],
    ) -> ToolResult:
        """Make a tool call on the model's behalf and thread it into the
        conversation exactly like one the model made itself."""
        tool_call_id = f"tc-{uuid.uuid4().hex[:8]}"
        self._set_phase(_phase_for(action_name), action_name)
        result = await self.tool_caller(topic, action_name, args)
        self.logger.log_tool_call(ToolCallRecord(
            ts=_now_iso(), step=self.record.steps_used, tool_call_id=tool_call_id,
            topic=topic, action=action_name, args=args, result=result,
        ))
        self._tool_calls_attempted += 1
        if result.status == "ok":
            self._tool_calls_succeeded += 1
        synthetic = ProposedAction(kind="tool", topic=topic, action=action_name, args=args)
        self._note_spoken(synthetic, result)
        self._note_conversation_io(synthetic, result)
        messages.append(ChatMessage(role="assistant", content="", tool_call_id=tool_call_id,
                                    name=action_name, tool_args=args))
        messages.append(ChatMessage(role="tool", content=_format_tool_result(result),
                                    tool_call_id=tool_call_id, name=f"{topic}::{action_name}"))
        return result

    async def _resume_listening(
        self, action: ProposedAction, messages: List[ChatMessage], deadline: float,
    ) -> bool:
        """resume_on_done: speak the ``done`` text if it's a reply the
        operator hasn't heard, then listen (re-listening on silence and
        echo, like loop_on_timeout) and hand the model the result. Returns
        False if the run was terminated (loop breaker / timeout)."""
        text = (action.rationale or "").strip()
        if text and text.lower() != "done" and self._last_speak_topic:
            last = self._recent_spoken[-1][1] if self._recent_spoken else ""
            if _echo_containment(_echo_words(text), _echo_words(last)) < 0.8:
                await self._synth_call(self._last_speak_topic, "speak",
                                       {"text": text, "wait": True}, messages)
                trip = self._loop_breaker_trip()
                if trip:
                    self._terminate_failure(f"loop breaker: {trip}")
                    return False
        topic, args = self._last_listen
        while True:
            if self._cancel_event.is_set():
                raise asyncio.CancelledError()
            if time.monotonic() > deadline:
                self._terminate_failure(
                    f"workflow.timeout_seconds={self.workflow.timeout_seconds} exceeded")
                return False
            # Not threaded into messages until it's a real utterance.
            self._set_phase("listening", "listen")
            result = await self.tool_caller(topic, "listen", args)
            listen_action = ProposedAction(kind="tool", topic=topic, action="listen", args=args)
            lot = self.workflow.loop_on_timeout
            silent = lot is not None and _matches_loop_on_timeout(result, lot)
            if silent or self._is_self_echo(listen_action, result):
                self.silent_listens += 1
                self.logger.log_tool_call(ToolCallRecord(
                    ts=_now_iso(), step=self.record.steps_used, tool_call_id=f"tc-{uuid.uuid4().hex[:8]}",
                    topic=topic, action="listen", args=args, result=result,
                ))
                trip = self._loop_breaker_trip()
                if trip:
                    self._terminate_failure(f"loop breaker: {trip}")
                    return False
                continue
            break
        tool_call_id = f"tc-{uuid.uuid4().hex[:8]}"
        self.logger.log_tool_call(ToolCallRecord(
            ts=_now_iso(), step=self.record.steps_used, tool_call_id=tool_call_id,
            topic=topic, action="listen", args=args, result=result,
        ))
        self._note_heard(listen_action, result)
        messages.append(ChatMessage(role="assistant", content="", tool_call_id=tool_call_id,
                                    name="listen", tool_args=args))
        messages.append(ChatMessage(role="tool", content=self._tool_result_content("listen", result),
                                    tool_call_id=tool_call_id, name=f"{topic}::listen"))
        return True

    def _note_heard(self, action: ProposedAction, result: ToolResult) -> None:
        """A ``listen`` that returned real operator text resets the
        loop breaker's consecutive-speak count."""
        if action.action != "listen" or result.status != "ok":
            return
        value = result.value if isinstance(result.value, dict) else {}
        if not value.get("timeout") and str(value.get("text") or "").strip():
            self._speaks_since_heard = 0
            self._refusal_nudged = False
            self.last_heard = str(value.get("text")).strip()
            self.last_heard_at = _now_iso()
            self.silent_listens = 0
            self._notify()

    def _loop_breaker_trip(self) -> Optional[str]:
        """Reason string when the run looks like a runaway, else None."""
        cfg = self.workflow.loop_breaker
        if not cfg.enabled:
            return None
        if cfg.max_consecutive_speaks and self._speaks_since_heard >= cfg.max_consecutive_speaks:
            return (f"spoke {self._speaks_since_heard} times without hearing the operator "
                    f"(max_consecutive_speaks={cfg.max_consecutive_speaks})")
        if cfg.max_echo_drops:
            now = time.monotonic()
            recent = [t for t in self._echo_drop_times if now - t <= cfg.echo_drop_window_seconds]
            self._echo_drop_times = recent
            if len(recent) >= cfg.max_echo_drops:
                return (f"heard its own voice {len(recent)} times in "
                        f"{cfg.echo_drop_window_seconds:.0f}s (max_echo_drops={cfg.max_echo_drops})")
        return None

    def _silence_speech(self) -> None:
        """At the end of a run that didn't finish normally (cancel, HALT,
        exit phrase, failure, loop breaker): stop every speech service this
        run spoke on, so queued replies don't keep playing after the run is
        gone. A normal ``done`` lets the last reply finish."""
        if self.record.status == "success" and not getattr(self, "_exit_via_phrase", False):
            return
        for topic in sorted(self._speech_topics):
            try:
                self._publish(topic, {"action": "stop"})
            except Exception:  # noqa: BLE001
                logger.exception("brain run %s: stopping speech on %s failed", self.run_id, topic)

    # ─── workflow_events publishing ────────────────────────────────

    def _emit_workflow_event(self, event: str) -> None:
        """Publish a workflow-level lifecycle event to
        /brain/<proxy_id>/workflow_events. Non-retained — each run's
        start/end is a discrete signal, not a state to query.

        Failures are swallowed; missed events don't affect correctness."""
        if not self._proxy_id:
            return
        topic = f"/brain/{self._proxy_id}/workflow_events"
        payload: Dict[str, Any] = {
            "event": event,
            "workflow": self.workflow.name,
            "run_id": self.run_id,
            "started_at": self.record.started_at,
        }
        if event == "started":
            payload["inputs"] = dict(self.inputs)
            payload["model"] = self.workflow.preferred_backend
        elif event == "ended":
            payload["ended_at"] = self.record.ended_at or _now_iso()
            payload["status"] = self.record.status
            payload["tool_calls_count"] = self._tool_calls_succeeded
            if self.record.result_summary:
                payload["result_summary"] = self.record.result_summary
            if self.record.failure_reason:
                payload["failure_reason"] = self.record.failure_reason
            # Duration in milliseconds — wall-clock is what operators
            # care about (vs eval_count token totals).
            try:
                from datetime import datetime
                started = datetime.fromisoformat(self.record.started_at)
                ended = datetime.fromisoformat(payload["ended_at"])
                payload["duration_ms"] = int(
                    (ended - started).total_seconds() * 1000
                )
            except Exception:  # noqa: BLE001
                payload["duration_ms"] = None
        try:
            self._publish(topic, payload)
        except Exception:  # noqa: BLE001
            logger.exception("workflow_events publish failed (%s)", event)

    def cancel(self, reason: str = "") -> None:
        """Request the engine to stop at the next safe checkpoint."""
        if reason:
            self.record.failure_reason = reason
        self._cancel_event.set()
        if self._approval_event is not None and not self._approval_event.is_set():
            self._approval_decision = False
            self._approval_event.set()

    # ─── exit-phrase watcher (E1) ──────────────────────────────────

    async def _exit_phrase_watcher(self) -> None:
        """Fan out one ``_watch_exit_topic`` per
        ``workflow.exit_on_phrase.listen_topics`` entry (usually just
        one — a list is only needed when more than one `messaging`-
        capability service type might be providing conversation I/O,
        see packages/rlx_messaging/README.md). Whichever topic sees a match first
        wins; cancelling ``run()``'s task cancels this watcher (and
        every sibling topic-watcher with it) via the normal
        ``run().finally`` path, so nothing needs manual cross-task
        signaling here.
        """
        cfg = self.workflow.exit_on_phrase
        if cfg is None or not cfg.listen_topics:
            return
        matches = list(cfg.matches or [])
        if not matches:
            return
        # Pre-normalize so the per-message hot path is just string
        # compare / contains.
        matches_norm = [m.lower() for m in matches] if cfg.case_insensitive else matches
        await asyncio.gather(*(
            self._watch_exit_topic(topic, f"brain-engine-{self.run_id}-exit-{i}", cfg, matches, matches_norm)
            for i, topic in enumerate(cfg.listen_topics)
        ))

    async def _watch_exit_topic(
        self, topic: str, sid: str, cfg: ExitOnPhrase, matches: List[str], matches_norm: List[str],
    ) -> None:
        """Subscribe to ONE topic and match every incoming text payload
        against the configured phrases. On match: set
        ``_exit_via_phrase`` + cancel the run task so an in-flight
        ``adapter.complete()`` aborts immediately — exit latency is
        bus-RTT, not inference-latency.

        The cancel manifests as ``CancelledError`` in ``run()``, which
        the special-case branch there converts to a clean
        ``status=success`` with a ``result_summary`` naming the
        matched phrase.

        Failures on THIS topic (bus issues, malformed payloads) are
        logged but swallowed — a broken watcher on one topic must not
        crash the run OR take down sibling topic-watchers.
        """
        try:
            async for msg in get_bus().subscribe(topic, subscriber_id=sid):
                payload = msg.payload if hasattr(msg, "payload") else msg
                if not isinstance(payload, dict):
                    continue
                text = payload.get("text")
                if not isinstance(text, str):
                    continue
                candidate = text.strip()
                candidate_norm = candidate.lower() if cfg.case_insensitive else candidate
                hit: Optional[str] = None
                for raw_m, norm_m in zip(matches, matches_norm):
                    if cfg.whole_message:
                        if candidate_norm == norm_m:
                            hit = raw_m
                            break
                    else:
                        if norm_m in candidate_norm:
                            hit = raw_m
                            break
                if hit is None:
                    continue
                self._exit_via_phrase = True
                self._exit_phrase_matched = hit
                logger.info(
                    "brain.workflow_engine: exit phrase %r matched on %s — cancelling run %s",
                    hit, topic, self.run_id,
                )
                if self._run_task is not None and not self._run_task.done():
                    self._run_task.cancel()
                return
        except asyncio.CancelledError:
            # Normal shutdown path — the run.finally cancels us.
            raise
        except Exception:  # noqa: BLE001
            logger.exception(
                "brain.workflow_engine: exit-phrase watcher crashed on topic=%s (run=%s)",
                topic, self.run_id,
            )

    def approve(self, decision: bool) -> bool:
        """Resolve a pending approval gate. Returns True if a gate
        was actually pending; False if there was nothing to approve."""
        if self._approval_event is None or self._approval_event.is_set():
            return False
        self._approval_decision = decision
        self._approval_event.set()
        return True

    # ─── step loop ─────────────────────────────────────────────────

    async def _loop(self, messages: List[ChatMessage], tools: List[ToolDescriptor]) -> None:
        deadline = self._deadline()
        while True:
            if self._cancel_event.is_set():
                raise asyncio.CancelledError()

            verdict = check_max_steps(self.workflow, self.record.steps_used)
            if not verdict.allowed:
                self._terminate_failure(verdict.reason)
                return
            verdict = check_max_tokens(self.workflow, self.record.tokens_used)
            if not verdict.allowed:
                self._terminate_failure(verdict.reason)
                return
            if time.monotonic() > deadline:
                self._terminate_failure(
                    f"workflow.timeout_seconds={self.workflow.timeout_seconds} exceeded"
                )
                return

            # Re-expand the tool list + capability index every
            # iteration for lazy_tools workflows — cheap, in-memory
            # (no bus calls), and picks up anything the model unlocked
            # via describe_capability on a prior step. No-op (same
            # list every time, no cost) for non-lazy workflows.
            if self.workflow.lazy_tools:
                tools = self._lazy_tool_descriptors()

            # ``self._model_id`` pins a specific model id for this
            # run (e.g. ``llama3.2:3b``). Resolved upstream from per-
            # call override > workflow.preferred_model > None. When
            # None the adapter uses its configured default. Per-call
            # so concurrent runs with different pins on the same
            # adapter instance don't trample each other.
            #
            # Check FastReflexEngine before calling the slow LLM
            response = None
            if self.workflow.fast_reflex and self._reflex_engine.enabled:
                # Only what the operator actually said (a listen result) —
                # never the workflow prompt, whose instructions read like
                # commands ("turn the switch on or off"). Each heard
                # utterance is evaluated once, keyed by when it was heard
                # so repeating the same command still works.
                candidate_text = self.last_heard
                heard_at = self.last_heard_at
                if candidate_text and heard_at != self._last_reflex_heard_at:
                    self._last_reflex_heard_at = heard_at
                    reflex = await self._reflex_engine.evaluate_utterance(candidate_text, self.tool_catalog)
                    if reflex and reflex.actions:
                        logger.info(
                            "FastReflexEngine bypassed LLM for '%s' (%s, conf=%.2f, lat=%.1fms)",
                            candidate_text,
                            reflex.reflex_kind,
                            reflex.confidence,
                            reflex.latency_ms or 0.0,
                        )
                        response = ModelResponse(
                            action=reflex.actions[0],
                            actions=reflex.actions,
                            raw={"fast_reflex": reflex.reflex_kind, "text": candidate_text},
                            usage=ModelUsage(),
                        )

            if response is None:
                # Log the exact request before dispatching. ``messages``
                # grows turn-over-turn as tool results are threaded back
                # in; capturing each call gives a full
                # request-stream-over-time view in
                # ``runs/<id>/requests.jsonl``. Pair with steps.jsonl's
                # ``response_raw`` for a complete request/response audit.
                self.logger.log_request({
                    "ts": _now_iso(),
                    "step": self.record.steps_used,
                    "backend": self.adapter.name,
                    "model": self._model_id,
                    "messages": [m.model_dump() for m in messages],
                    "tools": self.adapter.encode_tools_for_log(tools),
                })
                self._set_phase("thinking")
                response = await self.adapter.complete(
                    messages,
                    tools=tools,
                    model=self._model_id,
                )
                self.record.tokens_used += response.usage.input_tokens + response.usage.output_tokens

            # A response can carry several proposed actions — a model
            # that acts and confirms in the same turn (e.g. power_set +
            # speak) — see README.md "Several tool calls in one response". Execute
            # each in order within this one step (they share
            # self.record.steps_used; that counter is LLM round-trips,
            # not individual tool calls). Any terminal outcome (done,
            # disallowed, approval denied, terminate_on satisfied) ends
            # the whole run immediately, same as it always has —
            # remaining actions in the batch are simply never reached.
            for action in response.actions:
                verdict = check_tool_call(self.workflow, action, tool_catalog=self.tool_catalog)

                step_rec = StepRecord(
                    ts=_now_iso(),
                    step=self.record.steps_used,
                    model=self.adapter.name,
                    prompt_summary=_summarise_messages(messages),
                    response_raw=response.raw,
                    action=action,
                    verdict=verdict,
                    usage=response.usage,
                )

                if action.kind == "done":
                    self.logger.log_step(step_rec)
                    if (
                        self.workflow.exit_on_phrase is not None
                        and self.workflow.resume_on_done
                        and self._last_listen is not None
                    ):
                        # Mid-conversation "done" from a small model: say
                        # what it meant to say, then listen again — same
                        # run, same context. Counted once by the outer loop.
                        if not await self._resume_listening(action, messages, deadline):
                            return
                        break
                    self.record.steps_used += 1
                    # Phrase-terminated workflows (exit_on_phrase set) run
                    # until the operator says the configured phrase — they
                    # are NOT meant to end when the model decides it's
                    # finished. A model-emitted ``done`` here is a misfire,
                    # not a success. The usual cause: the model had no real
                    # tools to call (the conversation service isn't running,
                    # so listen/speak aren't in the catalog) so it narrated
                    # its intended calls as text, which parses as ``done``.
                    # Fail honestly instead of reporting a 0-work success.
                    if self.workflow.exit_on_phrase is not None:
                        # Name the ACTUAL model that ran so the message is
                        # truthful about what was sent (no assumptions about
                        # which model is configured).
                        model_desc = self.record.model_id or self.adapter.name
                        if self._tools_offered_count == 0:
                            why = ("no tools were available to the model — is a "
                                   "conversation service (chat, discord, slack, "
                                   "google_chat) running? listen/speak come from it")
                        elif self._tool_calls_succeeded == 0:
                            why = (f"the model ({model_desc}) returned prose "
                                   "instead of calling a tool — it must act via "
                                   "tools (listen/speak/write), not describe")
                        else:
                            why = (f"the model ({model_desc}) made "
                                   f"{self._tool_calls_succeeded} tool call(s) "
                                   "then stopped instead of continuing")
                        self._terminate_failure(
                            "model emitted 'done', but this workflow ends only "
                            f"on its exit phrase, not by the model. {why}."
                        )
                        return
                    # When ``terminate_on`` is declared, ``done`` is only
                    # legitimate AFTER the required actions have actually
                    # been called successfully. Without this check, models
                    # that emit ``done`` without calling any tools (text-
                    # as-JSON encoders like qwen2.5-coder, hallucinated
                    # completions) would silently succeed with no work
                    # done. Treat that as a failure — the workflow's stated
                    # success criterion wasn't met.
                    required = {c.action for c in self.workflow.terminate_on}
                    if required and not required.issubset(self._actions_succeeded):
                        missing = sorted(required - self._actions_succeeded)
                        self._terminate_failure(
                            f"model emitted done but terminate_on actions "
                            f"were not satisfied: missing {missing}"
                        )
                        return
                    self._terminate_success(action.rationale or "done")
                    return

                if not verdict.allowed:
                    self.logger.log_step(step_rec)
                    self.record.steps_used += 1
                    self._terminate_failure(f"unsafe tool call: {verdict.reason}")
                    return

                if self.workflow.requires_human_approval:
                    approved = await self._wait_for_approval()
                    if not approved:
                        self.logger.log_step(step_rec)
                        self.record.steps_used += 1
                        self._terminate_failure("operator denied approval")
                        return

                # Execute the tool call. tool_call_id stitches the response
                # back into the conversation as a `role=tool` message.
                tool_call_id = action.tool_call_id or f"tc-{uuid.uuid4().hex[:8]}"
                action.tool_call_id = tool_call_id
                step_rec.action = action
                step_rec.tool_call_id = tool_call_id
                self.logger.log_step(step_rec)

                if (
                    self.workflow.speak_wait
                    and action.action == "speak"
                    and _SPEECH_CONTROL_RE.match(action.topic or "")
                    and "wait" not in (action.args or {})
                ):
                    # Turn-taking: return once the reply has finished
                    # playing, not on enqueue — otherwise the next listen
                    # runs during playback and hears the robot itself.
                    action.args = {**(action.args or {}), "wait": True}

                self._set_phase(_phase_for(action.action), action.action)
                refusal = self._premature_refusal(action)
                if refusal is not None:
                    # Don't let the operator hear "I can't" when the
                    # capability is live but just not loaded yet.
                    result = ToolResult(status="error", error=refusal)
                else:
                    result = await self.tool_caller(action.topic or "", action.action or "", action.args)
                    if action.action == "describe_capability":
                        result = self._filter_described(result)
                self._note_spoken(action, result)
                self._note_conversation_io(action, result)
                tcr = ToolCallRecord(
                    ts=_now_iso(),
                    step=self.record.steps_used,
                    tool_call_id=tool_call_id,
                    topic=action.topic or "",
                    action=action.action or "",
                    args=action.args,
                    result=result,
                )
                self.logger.log_tool_call(tcr)
                self._tool_calls_attempted += 1
                if result.status == "ok":
                    self._tool_calls_succeeded += 1

                    # Progressive tool disclosure: a successful
                    # describe_capability call unlocks that capability's
                    # real tools starting next iteration (see the
                    # lazy_tools re-expand above). Only mark it
                    # discovered if it actually matched something —
                    # an empty/error result (bad capability name) stays
                    # hidden so the model can retry with a corrected
                    # name instead of silently "succeeding" at nothing.
                    if (
                        self.workflow.lazy_tools
                        and action.action == "describe_capability"
                        and isinstance(result.value, dict)
                        and result.value.get("actions")
                    ):
                        requested = (action.args or {}).get("capability")
                        if isinstance(requested, str) and requested:
                            self._discovered_prefixes.add(requested)
                            self._refresh_capability_index()

                    # Engine-driven termination: track which actions have
                    # been called successfully + check against the
                    # workflow's terminate_on requirements.
                    self._actions_succeeded.add(action.action or "")
                    required = {c.action for c in self.workflow.terminate_on}
                    if required and required.issubset(self._actions_succeeded):
                        self.record.steps_used += 1
                        self.logger.write_summary(self.record)
                        self._terminate_success(
                            f"terminate_on satisfied: {sorted(required)}"
                        )
                        return

                # E2 — loop_on_timeout: re-dispatch the SAME tool call
                # (no LLM round-trip, no steps_used increment) while the
                # result keeps matching the configured field/value. The
                # canonical case is ``chat.listen`` returning
                # ``{timeout: true}`` after its wait window — the engine
                # silently re-listens until the operator actually says
                # something, instead of pinging the LLM for a "you got
                # nothing, listen again" turn each time. Operator cancel
                # + wall-clock timeout still fire from inside the inner
                # loop. Each re-dispatch logs a fresh ToolCallRecord for
                # audit; the LLM only sees the final (non-matching)
                # result via the threaded ``tool`` message below.
                # The self-echo backstop rides the same re-dispatch: a listen
                # result that's just the robot's own recent speech is
                # dropped (the model never sees it) and listened again.
                lot_cfg = self.workflow.loop_on_timeout
                while (
                    (
                        lot_cfg is not None
                        and (action.action or "") == lot_cfg.tool
                        and _matches_loop_on_timeout(result, lot_cfg)
                    )
                    or self._is_self_echo(action, result)
                ):
                    if self._cancel_event.is_set():
                        raise asyncio.CancelledError()
                    trip = self._loop_breaker_trip()
                    if trip:
                        self._terminate_failure(f"loop breaker: {trip}")
                        return
                    if time.monotonic() > deadline:
                        self._terminate_failure(
                            f"workflow.timeout_seconds={self.workflow.timeout_seconds} exceeded"
                        )
                        return
                    if (action.action or "") == "listen":
                        self.silent_listens += 1
                        self._set_phase("listening", action.action)
                    redispatch_id = f"tc-{uuid.uuid4().hex[:8]}"
                    result = await self.tool_caller(
                        action.topic or "", action.action or "", action.args,
                    )
                    self.logger.log_tool_call(ToolCallRecord(
                        ts=_now_iso(),
                        step=self.record.steps_used,
                        tool_call_id=redispatch_id,
                        topic=action.topic or "",
                        action=action.action or "",
                        args=action.args,
                        result=result,
                    ))
                    self._tool_calls_attempted += 1
                    if result.status == "ok":
                        self._tool_calls_succeeded += 1

                self._note_heard(action, result)
                trip = self._loop_breaker_trip()
                if trip:
                    self._terminate_failure(f"loop breaker: {trip}")
                    return

                # Feed the tool result back into the conversation so the
                # next .complete() call sees it. The assistant message
                # carries the tool name + args so adapters that need the
                # full call shape (anthropic ``tool_use`` block, openai
                # ``tool_calls`` array) can reconstruct it. Without these
                # fields, anthropic's API returns 400 because the
                # tool_result's tool_use_id resolves to no prior tool_use.
                messages.append(ChatMessage(
                    role="assistant",
                    content=action.rationale or "",
                    tool_call_id=tool_call_id,
                    name=action.action,
                    tool_args=action.args,
                ))
                messages.append(ChatMessage(
                    role="tool",
                    content=self._tool_result_content(action.action, result),
                    tool_call_id=tool_call_id,
                    name=f"{action.topic}::{action.action}",
                ))

            # E3 — message_window_size: trim the conversation to keep
            # the LLM's context bounded for long-running workflows
            # (conversation_servo can run for hours; without trimming,
            # ``messages`` grows turn-over-turn into the model's
            # context cap). Always keep the head (system prompt +
            # initial user context_body — the workflow's prompt.md +
            # memory injection) so workflow instructions never fall
            # out of the window; trim only the conversational tail to
            # the last N messages. Drop leading tool-result orphans
            # whose paired assistant message was just trimmed — they
            # confuse adapters that expect tool_use → tool_result
            # adjacency.
            wnd = self.workflow.message_window_size
            if wnd is not None and wnd > 0 and len(messages) > 2 + wnd:
                head = messages[:2]
                tail = messages[2:][-wnd:]
                while tail and tail[0].role == "tool":
                    tail.pop(0)
                messages[:] = head + tail

            self.record.steps_used += 1
            self.logger.write_summary(self.record)

    # ─── session mind (claude_agent) ───────────────────────────────

    async def _session_loop(self, context_body: str, tools: List[ToolDescriptor]) -> None:
        """Run with a session mind (roadmap Phase 4): the session holds the
        conversation and decides which tools to call; the engine keeps the
        runtime. For a conversational workflow (``exit_on_phrase``) the
        engine does the listening itself — silent re-listens, the echo
        filter and the loop breaker run here, with no model turn — and
        hands the session one turn per real utterance. Every tool call the
        session makes comes back through ``_session_tool`` (safety gate,
        speak_wait, step log). A one-shot workflow gets a single turn."""
        mind = self.adapter
        listen = self._conversation_tool("listen", tools) if self.workflow.exit_on_phrase else None
        reply_speak = self._conversation_tool("speak", tools, near=listen.topic) if listen else None
        # The engine listens, and speaks the session's closing text — so the
        # session gets neither listen nor the reply channel's speak. With
        # speak on offer, a turn could end on a speak with no text; Claude
        # Code then injects "no visible output" and the model speaks again,
        # repeating itself until the loop breaker trips (live, 2026-10-04).
        offered = ([t for t in tools if t.action != "listen" and t is not reply_speak]
                   if listen else tools)
        self._tools_offered_count = len(offered)
        self._session_lock = asyncio.Lock()
        self._session_spoke = False
        note = _SESSION_NOTE if listen else _SESSION_TASK_NOTE
        system = _system_prompt(self.workflow, session=True) + "\n\n" + note + "\n\n" + context_body
        self._set_phase("starting", "connecting")
        await mind.open(system_prompt=system, tools=offered, handler=self._session_tool, model=self._model_id)
        deadline = self._deadline()
        try:
            if listen is None:
                turn = await self._session_turn(
                    "Begin. Carry out the workflow with your tools, then reply with a one-line summary.")
                if turn is None or self.record.status != "running":
                    return
                if turn.is_error:
                    self._terminate_failure(f"{mind.name}: {turn.error}")
                    return
                required = {c.action for c in self.workflow.terminate_on}
                if required and not required.issubset(self._actions_succeeded):
                    self._terminate_failure(
                        f"session ended but terminate_on actions were not satisfied: "
                        f"missing {sorted(required - self._actions_succeeded)}")
                    return
                self._terminate_success(turn.text or "done")
                return
            listen_args = {"timeout_seconds": int(self.inputs.get("listen_window_seconds") or 8)}
            while True:
                heard = await self._session_listen(listen, listen_args, deadline)
                if heard is None:
                    return
                turn = await self._session_turn(heard)
                if turn is None or self.record.status != "running":
                    return
                if turn.is_error:
                    self._terminate_failure(f"{mind.name}: {turn.error}")
                    return
                reply = (turn.text or "").strip()
                if reply and not self._session_spoke:
                    # The session's closing text IS its reply (see
                    # _SESSION_NOTE) — speak it, unless the turn ended on
                    # the session's own speak: then the closing text only
                    # restates it, often paraphrased, and the operator
                    # heard two answers.
                    if reply_speak is not None:
                        await self._session_tool(reply_speak, {"text": reply})
                if self.record.status != "running":
                    return
                trip = self._loop_breaker_trip()
                if trip:
                    self._terminate_failure(f"loop breaker: {trip}")
                    return
        finally:
            await mind.close()

    async def _session_turn(self, text: str):
        """One session turn, accounted like LLM steps."""
        if self._cancel_event.is_set():
            raise asyncio.CancelledError()
        self._session_spoke = False
        self._set_phase("thinking")
        self.logger.log_request({"ts": _now_iso(), "step": self.record.steps_used,
                                 "backend": self.adapter.name, "model": self._model_id, "message": text})
        turn = await self.adapter.turn(text)
        self.record.steps_used += max(1, turn.api_turns)
        self.record.tokens_used += turn.input_tokens + turn.output_tokens
        self.record.cost_usd = round((self.record.cost_usd or 0.0) + turn.cost_usd, 6)
        self.logger.log_step(StepRecord(
            ts=_now_iso(), step=self.record.steps_used, model=self.adapter.name,
            prompt_summary=f"session turn: {text[:80]!r}",
            response_raw={**turn.raw, "text": turn.text, "tool_calls": turn.tool_calls,
                          "cost_usd": turn.cost_usd, "error": turn.error},
            action=ProposedAction(kind="done", rationale=turn.text or ""),
            verdict=SafetyVerdict(allowed=True),
            usage=ModelUsage(input_tokens=turn.input_tokens, output_tokens=turn.output_tokens),
        ))
        self.logger.write_summary(self.record)
        if self.record.status == "running":
            verdict = check_max_steps(self.workflow, self.record.steps_used)
            if verdict.allowed:
                verdict = check_max_tokens(self.workflow, self.record.tokens_used)
            if not verdict.allowed:
                self._terminate_failure(verdict.reason)
        return turn

    async def _session_listen(self, listen: ToolDescriptor, args: Dict[str, Any], deadline: float) -> Optional[str]:
        """Listen until the operator really says something (silence and
        self-echo re-listen without a model turn). Returns the text to
        hand the session, or None if the run was terminated."""
        action = ProposedAction(kind="tool", topic=listen.topic, action="listen", args=args)
        while True:
            if self._cancel_event.is_set():
                raise asyncio.CancelledError()
            if time.monotonic() > deadline:
                self._terminate_failure(f"workflow.timeout_seconds={self.workflow.timeout_seconds} exceeded")
                return None
            self._set_phase("listening", "listen")
            result = await self.tool_caller(listen.topic, "listen", args)
            self.logger.log_tool_call(ToolCallRecord(
                ts=_now_iso(), step=self.record.steps_used, tool_call_id=f"tc-{uuid.uuid4().hex[:8]}",
                topic=listen.topic, action="listen", args=args, result=result,
            ))
            self._tool_calls_attempted += 1
            if result.status == "ok":
                self._tool_calls_succeeded += 1
            value = result.value if isinstance(result.value, dict) else {}
            text = str(value.get("text") or "").strip()
            lot = self.workflow.loop_on_timeout
            silent = (lot is not None and _matches_loop_on_timeout(result, lot)) or (result.status == "ok" and not text)
            if result.status != "ok":
                self.silent_listens += 1
                await asyncio.sleep(1.0)          # a broken listen must not spin
            elif silent or self._is_self_echo(action, result):
                self.silent_listens += 1
            else:
                self._note_heard(action, result)
                return self._tool_result_content("listen", result).removeprefix("OK ")
            trip = self._loop_breaker_trip()
            if trip:
                self._terminate_failure(f"loop breaker: {trip}")
                return None

    async def _session_tool(self, desc: ToolDescriptor, args: Dict[str, Any]):
        """A tool call made by the session mind: the same gate, speak_wait,
        bookkeeping and logging as ``_loop``. Serialized, so parallel calls
        from the session run (and are logged) one at a time."""
        async with self._session_lock:
            if self._cancel_event.is_set() or self.record.status != "running":
                return "ERROR the run has ended", True
            action = ProposedAction(kind="tool", topic=desc.topic, action=desc.action, args=dict(args),
                                    tool_call_id=f"tc-{uuid.uuid4().hex[:8]}")
            verdict = check_tool_call(self.workflow, action, tool_catalog=self.tool_catalog)
            self.logger.log_step(StepRecord(
                ts=_now_iso(), step=self.record.steps_used, model=self.adapter.name,
                prompt_summary="session tool call", action=action, verdict=verdict,
                tool_call_id=action.tool_call_id,
            ))
            if not verdict.allowed:
                return f"ERROR blocked by the safety gate: {verdict.reason}", True
            if self.workflow.requires_human_approval and not await self._wait_for_approval():
                return "ERROR the operator denied this call", True
            if (self.workflow.speak_wait and action.action == "speak"
                    and _SPEECH_CONTROL_RE.match(action.topic or "") and "wait" not in action.args):
                action.args = {**action.args, "wait": True}
            self._set_phase(_phase_for(action.action), action.action)
            result = await self.tool_caller(action.topic or "", action.action or "", action.args)
            self._note_spoken(action, result)
            self._note_conversation_io(action, result)
            # "The turn's latest tool call was a successful speak": a
            # speak followed by more work leaves room for a real answer in
            # the closing text; one that ended the turn already was it.
            self._session_spoke = action.action == "speak" and result.status == "ok"
            self.logger.log_tool_call(ToolCallRecord(
                ts=_now_iso(), step=self.record.steps_used, tool_call_id=action.tool_call_id,
                topic=action.topic or "", action=action.action or "", args=action.args, result=result,
            ))
            self._tool_calls_attempted += 1
            if result.status == "ok":
                self._tool_calls_succeeded += 1
                self._actions_succeeded.add(action.action or "")
                required = {c.action for c in self.workflow.terminate_on}
                if required and required.issubset(self._actions_succeeded):
                    self._terminate_success(f"terminate_on satisfied: {sorted(required)}")
                    self._cancel_event.set()
            trip = self._loop_breaker_trip()
            if trip and self.record.status == "running":
                self._terminate_failure(f"loop breaker: {trip}")
                self._cancel_event.set()
            self._set_phase("thinking")
            return self._tool_result_content(action.action, result), result.status != "ok"

    def _conversation_tool(self, action: str, tools: List[ToolDescriptor],
                           near: Optional[str] = None) -> Optional[ToolDescriptor]:
        """The ``listen``/``speak`` tool a session run uses: the audio one
        (stt/speech) when live, else the same channel as ``near``, else the
        first allowed."""
        cands = [t for t in tools if t.action == action]
        if not cands:
            return None
        prefer = "/speech/" if action == "speak" else "/stt/"
        if near and not near.startswith("/stt/"):
            prefer = near.rsplit("/", 2)[0] + "/"
        for t in cands:
            if t.topic.startswith(prefer):
                return t
        return cands[0]

    # ─── terminal transitions ──────────────────────────────────────

    def _terminate_success(self, summary: str) -> None:
        self.record.status = "success"
        self.record.ended_at = _now_iso()
        self.record.result_summary = summary
        body = (self.workflow.success_md or summary).strip()
        self.logger.write_result(body, status="success")

    def _terminate_failure(self, reason: str) -> None:
        self.record.status = "failure"
        self.record.ended_at = _now_iso()
        self.record.failure_reason = reason
        body = (self.workflow.failure_md or reason).strip()
        self.logger.write_result(body + ("\n\n" + reason if self.workflow.failure_md else ""), status="failure")

    # ─── helpers ───────────────────────────────────────────────────

    def _allowed_tool_descriptors(self) -> List[ToolDescriptor]:
        """Filter the live tool catalog down to entries the workflow
        allows. Adapters get this list — they never see anything else,
        so a buggy model can't propose a tool it wasn't shown."""
        out: List[ToolDescriptor] = []
        for descriptor in self.tool_catalog.values():
            v = check_tool_call(
                self.workflow,
                ProposedAction(
                    kind="tool",
                    topic=descriptor.topic,
                    action=descriptor.action,
                ),
            )
            if v.allowed:
                out.append(descriptor)
        return out

    # ─── runtime environment + clock ───────────────────────────────

    def _environment_lines(self) -> List[str]:
        """``# Environment`` section of the run header: local date/time
        at run start plus whatever the runtime reports about itself.
        The clock goes stale over a long run — ``stamp_time`` keeps it
        current on every heard utterance."""
        facts: Dict[str, Any] = {}
        if self._runtime_facts is not None:
            try:
                facts = dict(self._runtime_facts() or {})
            except Exception:  # noqa: BLE001 — never fail a run over this
                logger.exception("brain run %s: runtime_facts failed", self.run_id)
        lines = [
            "# Environment",
            f"- local time at run start: {_now_local()}",
            f"- timezone: {_local_tz_name()}",
        ]
        lines += [f"- {k}: {v}" for k, v in facts.items() if v not in (None, "")]
        return lines + [""]

    def _tool_result_content(self, action_name: Optional[str], result: ToolResult) -> str:
        """Tool result text for the model; a real heard utterance gets
        the current local time appended (workflow.stamp_time) so the
        model can answer "what time is it" mid-conversation."""
        content = _format_tool_result(result)
        if (
            self.workflow.stamp_time
            and action_name == "listen"
            and result.status == "ok"
            and isinstance(result.value, dict)
            and not result.value.get("timeout")
            and str(result.value.get("text") or "").strip()
        ):
            content += f"\n[now: {_now_local()}]"
        return content

    def _context_message_text(self) -> str:
        if self._context_index:
            return self._context_head + "\n\n" + self._context_index
        return self._context_head

    def _refresh_capability_index(self) -> None:
        """Rewrite the capability index in the context message after a
        discovery, so it stops listing capabilities that are now loaded."""
        index = self._capability_index_text()
        if index == self._context_index:
            return
        self._context_index = index
        if self._context_msg is not None:
            self._context_msg.content = self._context_message_text()

    def _filter_described(self, result: ToolResult) -> ToolResult:
        """describe_capability reports every live tool under a prefix;
        drop the ones this workflow may not call so the model never sees
        schemas for blocked actions (they'd only bounce off the gate)."""
        value = result.value
        if result.status != "ok" or not isinstance(value, dict) or not value.get("actions"):
            return result
        kept = []
        for a in value["actions"]:
            if not isinstance(a, dict):
                continue
            v = check_tool_call(self.workflow, ProposedAction(
                kind="tool", topic=a.get("topic"), action=a.get("action"),
            ))
            if v.allowed:
                kept.append(a)
        if len(kept) == len(value["actions"]):
            return result
        new_value = {**value, "actions": kept, "topics": sorted({a.get("topic") for a in kept})}
        if not kept:
            new_value["error"] = (
                f"capability {value.get('capability')!r} is not available to this workflow"
            )
        return result.model_copy(update={"value": new_value})

    # ─── progressive tool disclosure (workflow.lazy_tools) ──────────

    def _always_loaded_prefixes(self) -> set:
        """Capability prefixes whose tools are never lazy-hidden:
        whatever the workflow names explicitly, plus 'brain' always
        (unconditionally — describe_capability itself lives there, and
        a workflow that couldn't see it would have no way to discover
        anything else; this isn't configurable)."""
        return set(self.workflow.always_loaded_prefixes) | {"brain"}

    def _lazy_tool_descriptors(self) -> List[ToolDescriptor]:
        """Narrow ``_allowed_tool_descriptors()`` further for
        ``lazy_tools`` workflows: only the always-loaded prefixes plus
        whatever's been discovered via ``describe_capability`` so far
        this run. No-op (returns the full allowed list unchanged) when
        ``workflow.lazy_tools`` is False — every existing workflow's
        behavior is untouched."""
        full = self._allowed_tool_descriptors()
        if not self._lazy_active(full):
            return full
        visible = self._always_loaded_prefixes() | self._discovered_prefixes
        return [d for d in full if capability_prefix(d.topic) in visible]

    def _lazy_active(self, full: List[ToolDescriptor]) -> bool:
        """Lazy disclosure only pays off for a big catalog — below
        ``lazy_tools_threshold`` actions everything is simply loaded."""
        if not self.workflow.lazy_tools:
            return False
        limit = self.workflow.lazy_tools_threshold
        return limit <= 0 or len(full) > limit

    def _hidden_capabilities(self) -> List[str]:
        """Capability prefixes that are live + allowed but not loaded."""
        full = self._allowed_tool_descriptors()
        if not self._lazy_active(full):
            return []
        visible = self._always_loaded_prefixes() | self._discovered_prefixes
        return sorted({capability_prefix(d.topic) for d in full} - visible)

    def _capability_index_text(self) -> str:
        """Compact, non-invokable summary of capabilities NOT currently
        expanded into real tool entries — what a lazy_tools workflow's
        prompt needs instead of their full schemas, so the model knows
        to call ``describe_capability`` rather than assuming nothing
        else exists. Empty string (nothing to add) for non-lazy
        workflows or when every live capability is already visible."""
        full = self._allowed_tool_descriptors()
        if not self._lazy_active(full):
            return ""
        visible = self._always_loaded_prefixes() | self._discovered_prefixes
        hidden_topics: Dict[str, set] = {}
        for d in full:
            prefix = capability_prefix(d.topic)
            if prefix in visible:
                continue
            hidden_topics.setdefault(prefix, set()).add(d.topic)
        if not hidden_topics:
            return ""
        lines = [
            "## Other live capabilities (not loaded in detail yet)",
            "",
            "Call `describe_capability(capability=<name>)` to get full "
            "actions + arguments for one of these before trying to use "
            "it — they aren't directly callable until you do:",
            "",
        ]
        for prefix, topics in sorted(hidden_topics.items()):
            lines.append(f"- **{prefix}** — {len(topics)} instance(s): {', '.join(sorted(topics))}")
        return "\n".join(lines)

    async def _wait_for_approval(self) -> bool:
        self._approval_decision = None
        self._approval_event = asyncio.Event()
        self.record.status = "awaiting_approval"
        self.logger.write_summary(self.record)
        try:
            await self._approval_event.wait()
        finally:
            self.record.status = "running"
            self.logger.write_summary(self.record)
        return bool(self._approval_decision)


# ─── small helpers ──────────────────────────────────────────────────


def _system_prompt(wf: Workflow, session: bool = False) -> str:
    """Default system prompt — adapters may augment per their format. A
    session mind ends its turns with text, so it gets no ``done`` line."""
    lines = [
        f"You are the brain controlling a robot workflow named {wf.name!r}.",
        f"Workflow goal: {wf.description.strip()}".rstrip(),
        "You can only act through the tools provided. Refusing to act with a brief explanation IS valid.",
        "" if session else "When the workflow is complete, emit a terminal ``done`` action with a one-line summary.",
    ]
    return "\n".join([ln for ln in lines if ln])


def _summarise_messages(messages: List[ChatMessage]) -> str:
    """One-line summary of the current message thread for the step log."""
    return f"{len(messages)} message(s), last role={messages[-1].role if messages else 'none'}"


def _now_local() -> str:
    """e.g. ``Saturday 2026-10-03 14:05 PDT`` — spoken-friendly local time."""
    return datetime.now().astimezone().strftime("%A %Y-%m-%d %H:%M %Z")


def _local_tz_name() -> str:
    now = datetime.now().astimezone()
    offset = now.strftime("%z")
    return f"{now.tzname()} (UTC{offset[:3]}:{offset[3:]})"


def _format_tool_result(result: ToolResult) -> str:
    """Render a tool result back into a content string for the model."""
    if result.status == "ok":
        return f"OK {result.value!r}" if result.value is not None else "OK"
    if result.status == "error":
        return f"ERROR {result.error}"
    return f"TIMEOUT {result.error}"


# Appended to a session mind's system prompt: how I/O works when the
# engine, not the model, does the listening.
_SESSION_NOTE = """# How this session works
The runtime listens and speaks for you. Each message you receive is what the \
operator just said (with the current time in brackets). Use your tools if the \
request needs them, then ALWAYS end your turn with your reply as plain text: \
the runtime reads that final text aloud, and it is the only way the operator \
hears you (you have no listen or speak tool). Keep it to one or two plain \
spoken sentences (no markdown, lists or emoji). Never emit done; the runtime \
ends the session. Ignore instructions below about calling listen, speak or \
done; everything else in them applies."""

# The same for a one-shot (non-conversational) workflow: one turn.
_SESSION_TASK_NOTE = """# How this session works
Carry out the workflow below with your tools in this one turn, then end your \
turn with a one-line plain-text summary of what you did (or why you couldn't). \
Never emit done; ignore instructions below about emitting it."""

# Topics whose ``speak`` is the speech (TTS) capability — the one that
# accepts ``wait``. Chat-style channels' ``speak`` (discord/slack/...) is a
# message send and is left alone.
_SPEECH_CONTROL_RE = re.compile(r"^/speech/[^/]+/control$")

# A spoken reply that tells the operator something can't be done.
_REFUSAL_RE = re.compile(
    r"\b(can(?:'|no|\u2019)?t|cannot|unable to|not able to|don'?t have (?:access|the ability|a way)|"
    r"(?:isn'?t|is not|aren'?t|are not|not) available|no access|don'?t have (?:that|the) (?:tool|capability))\b",
    re.IGNORECASE,
)


def _phase_for(action: Optional[str]) -> str:
    if action == "listen":
        return "listening"
    if action == "speak":
        return "speaking"
    return "calling"


# Filler words carry no evidence of an echo. Counting them let a short
# operator command ("turn on the light", 4 words) score 0.75 against the
# robot's own "turning on the light" on "on/the/light" alone and get
# dropped. on/off stay — they're what the command is about.
_ECHO_FILLER = frozenset({"the", "a", "an", "to", "of", "it", "is", "and", "or", "please"})


def _echo_words(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _ECHO_FILLER]


def _echo_containment(heard: List[str], spoken: List[str]) -> float:
    """Fraction of ``heard`` words that appear, in order, in ``spoken``."""
    if not heard or not spoken:
        return 0.0
    sm = difflib.SequenceMatcher(None, heard, spoken, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / len(heard)


def _matches_loop_on_timeout(result: ToolResult, cfg) -> bool:
    """True when ``result.value`` is a dict whose ``cfg.field`` matches
    ``cfg.value``. Tolerates non-dict payloads (returns False) so a
    misbehaving tool can't trap the engine in the re-dispatch inner
    loop."""
    val = result.value
    if not isinstance(val, dict):
        return False
    return val.get(cfg.field) == cfg.value
