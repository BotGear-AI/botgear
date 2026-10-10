"""run_workflow / run_skill: the whole orchestration, start to finish.

  load workflow → resolve inputs/skills → policy gate → build workspace →
  stream agent events to events.jsonl → run external checks →
  on_success (only if every check passed) → result.json

Statuses: completed (agent finished, every check passed), partial (agent
stopped early, e.g. a timeout, after writing some outputs), failed_checks
(agent finished, a check failed), cancelled (the caller's `cancel` event was
set; on_success never runs), error (nothing usable). The checks decide
whether outputs are usable, not whether the agent finished: on_success runs
for a partial run too when every check passed on what it wrote, and gets
the RunResult so it can tell the two apart.

Never raises for expected failures (bad input, policy refusal, agent crash):
those come back as RunResult(status="error"), so callers running this in a
background task get a result to record instead of a traceback. The caller
owns persistence beyond the workspace (DB rows, UI status, audit logs).
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from agent_harness.checks import CheckResult, run_checks
from agent_harness.events import AgentEvent
from agent_harness.policy import Policy
from agent_harness.pricing import estimate_cost
from agent_harness.runtime import AgentTask, get_driver
from agent_harness.state import RunState
from agent_harness.workflow import (Workflow, load_workflow, render_task, resolve_inputs, resolve_mcp,
                                    resolve_skills, self_check_commands, skill_workflow, substitute)
from agent_harness.workspace import Workspace, build_workspace, make_run_name

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    status: str                      # completed | partial | failed_checks | cancelled | error
    workflow: str
    agent: str
    workspace: Optional[str] = None
    model: Optional[str] = None
    session_id: Optional[str] = None
    duration_s: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: Optional[float] = None
    checks_passed: int = 0
    checks_total: int = 0
    outputs: list[str] = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    final_message: str = ""
    error: Optional[str] = None
    handled: Any = None              # what on_success returned, if it ran

    @property
    def ok(self) -> bool:
        return self.status == "completed"


def run_workflow(
    workflow: str,
    agent: str,
    inputs: dict[str, Any],
    *,
    workspace_root: Path,
    workflow_paths: Iterable[Path],
    skill_paths: Iterable[Path],
    **options: Any,
) -> RunResult:
    """Run a WORKFLOW.md found on `workflow_paths`. See `_execute` for options."""
    try:
        wf = load_workflow(workflow, workflow_paths)
    except Exception as exc:
        return RunResult(status="error", workflow=workflow, agent=agent, error=str(exc))
    return _execute(wf, agent, inputs, workspace_root=workspace_root, skill_paths=skill_paths, **options)


def run_skill(
    skill: str,
    agent: str,
    inputs: dict[str, Any],
    *,
    workspace_root: Path,
    skill_paths: Iterable[Path],
    outputs: Optional[list[str]] = None,
    optional_outputs: Optional[list[str]] = None,
    instructions: str = "",
    checks: Optional[list[str]] = None,
    secret_names: Optional[list[str]] = None,
    extra_skills: Optional[list[str]] = None,
    **options: Any,
) -> RunResult:
    """Run ONE skill as the whole task, without a WORKFLOW.md. Path-valued
    inputs appear under inputs/<name>; `outputs` (default output/OUTPUT.md)
    must be written under output/, `optional_outputs` are asked for but not
    required. Same options as run_workflow."""
    skill_paths = [Path(p) for p in skill_paths]
    try:
        skill_dir = resolve_skills([skill], skill_paths)[skill]
        wf = skill_workflow(skill, skill_dir, inputs=inputs, outputs=outputs or ["output/OUTPUT.md"],
                            optional_outputs=optional_outputs, instructions=instructions, checks=checks, policy=options.pop("workflow_policy", None),
                            secrets=secret_names or sorted((options.get("secrets") or {}).keys()),
                            extra_skills=extra_skills)
    except Exception as exc:
        return RunResult(status="error", workflow=skill, agent=agent, error=str(exc))
    wf_inputs = {k: (str(v) if isinstance(v, Path) else v) for k, v in inputs.items()}
    return _execute(wf, agent, wf_inputs, workspace_root=workspace_root, skill_paths=skill_paths,
                    snapshot_workflow=False, **options)


def _execute(
    wf: Workflow,
    agent: str,
    inputs: dict[str, Any],
    *,
    workspace_root: Path,
    skill_paths: Iterable[Path],
    agents_md: str = "",
    agent_config: Optional[dict[str, Any]] = None,
    policy: Optional[dict[str, Any]] = None,
    secrets: Optional[dict[str, str]] = None,
    python: str = sys.executable,
    run_name: Optional[str] = None,
    on_event: Optional[Callable[[AgentEvent], None]] = None,
    on_success: Optional[Callable[[RunResult], Any]] = None,
    prices: Optional[dict[str, dict[str, float]]] = None,
    snapshot_workflow: bool = True,
    cancel: Optional[threading.Event] = None,
) -> RunResult:
    """Options:
      agents_md      repo-wide instructions for the agent (AGENTS.md text)
      agent_config   driver options, passed through untranslated
      policy         overrides on top of the workflow's own policy
      secrets        {ENV_NAME: value} for names the workflow declares; they
                     reach only the agent's process environment and are
                     redacted from events.jsonl and result.json
      on_event       callback per AgentEvent (live progress)
      on_success     callback(RunResult) run ONLY when every check passed
                     (status completed, or partial: see the module doc),
                     e.g. writing results into the caller's records; its
                     return value lands in RunResult.handled. If it raises,
                     the run is recorded as status="error" with that message
      cancel         a threading.Event; setting it (from any thread) stops the
                     agent and ends the run as status="cancelled"
      prices         price table for cost estimates (drivers that report
                     their own cost don't need it)
    """
    started = time.time()
    result = RunResult(status="error", workflow=wf.name, agent=agent)
    ws: Optional[Workspace] = None
    state: Optional[RunState] = None
    secrets = {k: str(v) for k, v in (secrets or {}).items() if v not in (None, "")}
    redact = _redactor(secrets.values())
    try:
        undeclared = sorted(set(secrets) - set(wf.secrets))
        if undeclared:
            raise ValueError(f"secrets not declared by {wf.name!r}: {undeclared}")
        resolved_inputs = resolve_inputs(wf, inputs)
        skills = resolve_skills(wf.skills, skill_paths)
        run_policy = Policy.from_dict(wf.policy, policy)
        runtime = get_driver(agent, agent_config)
        run_policy.check_runtime(runtime)
        run_policy.check_mcp(runtime, list(wf.mcp.values()))

        path_inputs = {n for n, s in wf.inputs.items() if s.type == "path"}
        ws = build_workspace(
            Path(workspace_root), run_name or make_run_name(wf.name, agent),
            inputs=resolved_inputs, path_inputs=path_inputs, skills=skills, agents_md=agents_md,
            workflow_dir=wf.path if snapshot_workflow else None,
        )
        result.workspace = str(ws.root)
        placeholders = {"python": python, "workflow_dir": wf.path, "workspace": ws.root,
                        **{f"input.{k}": (f"inputs/{k}" if k in path_inputs else v) for k, v in resolved_inputs.items()}}

        prompt = render_task(wf, resolved_inputs, skills, placeholders, tools=runtime.capabilities.tools,
                             self_check=snapshot_workflow)
        ws.task_path.write_text(prompt, encoding="utf-8")

        state = RunState(workflow=wf.name, agent=agent, status="running", stages=wf.stages,
                         inputs={k: str(v) for k, v in resolved_inputs.items()})
        state.save(ws.root)

        task = AgentTask(
            prompt=prompt,
            outputs=wf.all_outputs,
            system_context=agents_md,
            skills={name: ws.skill_dir(name) for name in skills},
            read_dirs=[_readable_dir(resolved_inputs[n]) for n in path_inputs if n in resolved_inputs],
            context_commands=[{**c, "command": substitute(c.get("command", ""), placeholders)}
                              for c in wf.context_commands],
            python=python,
            env=secrets,
            self_checks=self_check_commands(wf, placeholders) if snapshot_workflow else [],
            mcp_servers=resolve_mcp(wf, placeholders),
        )

        agent_failed: Optional[str] = None
        finished = threading.Event()
        if cancel is not None:
            def watch() -> None:
                while not finished.is_set():
                    if cancel.wait(0.5):
                        runtime.cancel()
                        return
            threading.Thread(target=watch, name="agent-harness-cancel", daemon=True).start()
        events_file = ws.events_path.open("a", encoding="utf-8")
        try:
            events = iter(()) if cancel is not None and cancel.is_set() else runtime.run(ws.root, task, run_policy)
            for event in events:
                events_file.write(redact(event.to_json()) + "\n")
                events_file.flush()
                _absorb_event(event, result, state)
                if event.type == "error" and event.data.get("fatal"):
                    agent_failed = redact(str(event.data.get("message") or "agent failed"))
                if on_event:
                    on_event(event)
        finally:
            finished.set()
            events_file.close()
        state.save(ws.root)

        checks: list[CheckResult] = run_checks(ws.root, wf.outputs, wf.checks, placeholders,
                                               env=secrets, redact=redact)
        result.checks_total = len(checks)
        result.checks_passed = sum(1 for c in checks if c.passed)
        result.outputs = [o for o in wf.all_outputs if (ws.root / o).is_file()]
        result.deliverables = [o for o in wf.deliverables if o in result.outputs]
        failures = "; ".join(_failure_summary(c) for c in checks if not c.passed)

        if cancel is not None and cancel.is_set():
            result.status, result.error = "cancelled", "cancelled"
        elif agent_failed:
            # The agent stopped (timeout, crash) but may have written some
            # outputs first; say so instead of hiding them behind "error".
            result.status = "partial" if result.outputs else "error"
            result.error = agent_failed + (f"; {failures}" if failures and result.outputs else "")
        elif failures:
            result.status = "failed_checks"
            result.error = failures
        else:
            result.status = "completed"
        if on_success is not None and result.status in ("completed", "partial") and not failures:
            result.handled = on_success(result)
    except Exception as exc:
        logger.warning("agent_harness run failed: %s\n%s", redact(str(exc)), traceback.format_exc())
        result.status, result.error = "error", redact(str(exc))
    finally:
        result.duration_s = round(time.time() - started, 1)
        result.cost_usd = estimate_cost(result.model, result.usage, prices)
        if ws is not None:
            if state is not None:
                state.status = result.status
                state.error = result.error
                state.refresh_from_workspace(ws.root)
                state.save(ws.root)
            (ws.root / "result.json").write_text(
                redact(json.dumps(asdict(result), indent=2, default=str)), encoding="utf-8")
    return result


def _readable_dir(value: Any) -> Path:
    """Directory the agent must be allowed to read for a path input (a file
    input's parent)."""
    path = Path(value).resolve()
    return path if path.is_dir() else path.parent


def _redactor(values: Iterable[str]) -> Callable[[str], str]:
    secrets = sorted((v for v in values if len(v) >= 4), key=len, reverse=True)

    def redact(text: str) -> str:
        for value in secrets:
            text = text.replace(value, "***")
        return text
    return redact


def _failure_summary(check: CheckResult) -> str:
    """A check's own FAIL lines if it printed any (they say what's wrong),
    else its first output line, else its name."""
    lines = [ln.strip() for ln in check.detail.splitlines() if ln.strip()]
    fails = [ln for ln in lines if ln.upper().startswith("FAIL")]
    return " | ".join(fails) if fails else (lines[0] if lines else check.name)


def _absorb_event(event: AgentEvent, result: RunResult, state: RunState) -> None:
    data = event.data
    if event.type == "status":
        result.model = data.get("model") or result.model
        if data.get("session_id"):
            result.session_id = state.session_id = data["session_id"]
    elif event.type == "usage":
        result.usage.update(data)
    elif event.type == "completed":
        result.final_message = data.get("message") or result.final_message
