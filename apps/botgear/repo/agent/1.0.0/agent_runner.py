# unmanaged
"""agent service — runs agent_harness workflows from the bus.

Minutes-scale, checked agent work on the live robot (e.g. ``robot-health``),
executed by a rented agent (Claude Code) through ``packages/agent_harness``.
The agent reaches the robot only via botgear's MCP gateway, and each
workflow's checks verify its claims against the robot before a run counts
as ``completed``. See README.md and docs/AGENT_ROADMAP.md (Phase 3).

Bus surface (``/agent/<id>/control``):

    list_workflows()                          -> [{name, description, inputs, outputs, mcp, ...}]
    start_run(workflow, inputs={}, agent=None, model=None)   -> {run_id, status: running}
    cancel_run(run_id)                        -> {cancelled}
    get_run(run_id)                           -> run summary (+ deliverable text when done)
    wait_run(run_id, timeout_seconds=20)      -> get_run, after the run ends or the timeout
    list_runs(limit=20)                       -> active + recent runs
    get_run_log(run_id, limit=200)            -> {events, checks} from the run's workspace

Topics: ``state`` (retained: config summary, active runs with live phase,
recent runs), ``runs/<run_id>/events`` (each agent event, secrets
redacted), ``runs/<run_id>/result`` (retained: status, checks, cost,
deliverables).

Conventions: a workflow input named ``endpoint`` / ``ca_cert`` that the
caller leaves out is filled with this runtime's MCP endpoint / TLS cert;
a declared secret ``BOTGEAR_MCP_KEY`` gets ``mcp_api_key``. Cloud agents
run only with ``residency: cloud_ok`` (default ``local_only``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field, SecretStr
from rlx_bus import ServiceConfig

from botgear.framework import Service, service_method

logger = logging.getLogger(__name__)

BUNDLED = Path(__file__).resolve().parent / "content"
MCP_KEY_SECRET = "BOTGEAR_MCP_KEY"
_DELIVERABLE_MAX = 20_000
_EVENT_TEXT_MAX = 400


class AgentConfig(ServiceConfig):
    default_agent: str = Field("claude_code", description="Agent (agent_harness driver) used when start_run doesn't name one.")
    model: Optional[str] = Field(None, description="Model for the agent (default: the agent's own).")
    residency: Literal["local_only", "cloud_ok"] = Field(
        "local_only", description="local_only refuses agents that send data off-host (Claude Code). cloud_ok lets the robot's state go to the model — an explicit opt-in.")
    mcp_api_key: Optional[SecretStr] = Field(
        None, description="API client secret runs use to reach this robot over the MCP gateway (UI: Tools → API Clients). Its profile applies.")
    mcp_endpoint: Optional[str] = Field(None, description="MCP gateway URL for runs. Default: this backend's /v1/mcp on localhost.")
    ca_cert: Optional[str] = Field(None, description="PEM the agent trusts for the gateway's TLS. Default: the backend's ssl/cert.pem.")
    claude_cli: Optional[str] = Field(None, description="Path to the claude CLI if it isn't on the backend's PATH.")
    announce_topic: Optional[str] = Field(
        None, description="Speech control topic to announce finished runs on, e.g. /speech/mouth/control. Empty = silent.")
    max_concurrent_runs: int = Field(2, ge=1, le=8)
    keep_runs: int = Field(20, ge=1, le=500, description="Run workspaces kept per workflow.")
    workspace_path: Optional[str] = Field(None, description="Override for <data_dir>/agent/<id>/.")


@dataclass
class _Run:
    run_id: str
    workflow: str
    agent: str
    run_name: str
    started_at: str
    cancel: threading.Event = field(default_factory=threading.Event)
    task: Optional[asyncio.Task] = None
    status: str = "running"
    phase: str = "starting"
    phase_detail: Optional[str] = None
    events: int = 0
    tool_calls: int = 0
    last_message: str = ""
    secrets: List[str] = field(default_factory=list)
    summary: Optional[Dict[str, Any]] = None

    def live(self) -> Dict[str, Any]:
        return {"run_id": self.run_id, "workflow": self.workflow, "agent": self.agent,
                "status": self.status, "phase": self.phase, "phase_detail": self.phase_detail,
                "started_at": self.started_at, "events": self.events, "tool_calls": self.tool_calls,
                "last_message": self.last_message}


class AgentService(Service):
    config_class = AgentConfig

    # ─── lifecycle ───────────────────────────────────────────────────

    async def on_start(self) -> None:
        self._runs: Dict[str, _Run] = {}
        # Summaries of recently finished runs — the only record of a run
        # refused before it got a workspace (e.g. by the residency gate).
        self._finished: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._loop = asyncio.get_running_loop()
        self._publish_pending = False
        self._workspace = self._resolve_workspace()
        for sub in ("workflows", "skills", "runs"):
            (self._workspace / sub).mkdir(parents=True, exist_ok=True)
        self._control_task = asyncio.create_task(self.run_control_loop())
        await self._publish_state()

    async def on_stop(self) -> None:
        for run in self._runs.values():
            run.cancel.set()
        task = getattr(self, "_control_task", None)
        if task is not None:
            task.cancel()

    async def on_halt(self, event: Dict[str, Any]) -> None:
        """Runtime-wide HALT: cancel every run (the agent process is
        stopped; its run ends ``cancelled``)."""
        for run in list(self._runs.values()):
            run.cancel.set()
        if self._runs:
            logger.warning("agent %s: HALT cancelled %d run(s)", self.proxy_id, len(self._runs))

    # ─── paths + config ──────────────────────────────────────────────

    def _resolve_workspace(self) -> Path:
        if self.config.workspace_path:
            return Path(self.config.workspace_path).expanduser().resolve()
        from config import get_settings
        data_dir = Path(getattr(get_settings(), "data_dir", None) or "data")
        if not data_dir.is_absolute():
            data_dir = Path.cwd() / data_dir
        return (data_dir / "agent" / self.proxy_id).resolve()

    def _workflow_paths(self) -> List[Path]:
        """Per-instance workflows shadow the bundled ones (first match wins)."""
        return [self._workspace / "workflows", BUNDLED / "workflows"]

    def _skill_paths(self) -> List[Path]:
        return [self._workspace / "skills", BUNDLED / "skills"]

    def _agents_md(self) -> str:
        for path in (self._workspace / "AGENT_CONTEXT.md", BUNDLED / "AGENT_CONTEXT.md"):
            if path.is_file():
                return path.read_text(encoding="utf-8")
        return ""

    def _mcp_endpoint(self) -> str:
        if self.config.mcp_endpoint:
            return self.config.mcp_endpoint
        try:
            from config import get_settings
            s = get_settings()
            port = getattr(s, "port", None) or 8998
            scheme = "https" if getattr(s, "ssl_enabled", True) else "http"
        except Exception:  # noqa: BLE001
            port, scheme = 8998, "https"
        return f"{scheme}://localhost:{port}/v1/mcp"

    def _ca_cert(self) -> Optional[str]:
        if self.config.ca_cert:
            return self.config.ca_cert
        default = Path("ssl/cert.pem").resolve()
        return str(default) if default.is_file() else None

    def _mcp_key(self) -> Optional[str]:
        key = self.config.mcp_api_key
        if isinstance(key, SecretStr):
            key = key.get_secret_value()
        return key or None

    # ─── workflows ───────────────────────────────────────────────────

    def _load(self, name: str):
        from agent_harness.workflow import load_workflow
        return load_workflow(name, self._workflow_paths())

    def _workflow_names(self) -> List[str]:
        names: List[str] = []
        for base in self._workflow_paths():
            if base.is_dir():
                for d in sorted(base.iterdir()):
                    if (d / "WORKFLOW.md").is_file() and d.name not in names:
                        names.append(d.name)
        return names

    @service_method("list_workflows")
    async def m_list_workflows(self) -> List[Dict[str, Any]]:
        """The workflows this service can run, with their inputs (marked
        ``auto`` when the service fills them), outputs and live tools."""
        out = []
        for name in self._workflow_names():
            try:
                wf = self._load(name)
            except Exception as exc:  # noqa: BLE001
                out.append({"name": name, "error": str(exc)})
                continue
            out.append({
                "name": wf.name,
                "description": wf.description,
                "inputs": {n: {"type": s.type, "required": s.required, "description": s.description,
                               "auto": n in ("endpoint", "ca_cert")} for n, s in wf.inputs.items()},
                "outputs": wf.all_outputs,
                "deliverables": wf.deliverables,
                "mcp": {s.name: s.tools for s in wf.mcp.values()},
                "secrets": wf.secrets,
            })
        return out

    # ─── runs ────────────────────────────────────────────────────────

    @service_method("start_run")
    async def m_start_run(
        self,
        workflow: str,
        inputs: Optional[Dict[str, Any]] = None,
        agent: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Start ``workflow`` in the background; returns its ``run_id`` at
        once. A run takes minutes: follow it with ``get_run`` / ``wait_run``
        or the ``runs/<run_id>/events`` topic."""
        if _halted():
            return {"error": "runtime is HALTED — resume it before starting a run"}
        try:
            wf = self._load(workflow)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"no runnable workflow {workflow!r}: {exc}"}
        active = [r for r in self._runs.values() if r.status == "running"]
        if len(active) >= self.config.max_concurrent_runs:
            return {"error": f"{len(active)} run(s) already running (max_concurrent_runs={self.config.max_concurrent_runs})"}
        agent = agent or self.config.default_agent
        filled = dict(inputs or {})
        if "endpoint" in wf.inputs and not filled.get("endpoint"):
            filled["endpoint"] = self._mcp_endpoint()
        if "ca_cert" in wf.inputs and not filled.get("ca_cert") and self._ca_cert():
            filled["ca_cert"] = self._ca_cert()
        secrets: Dict[str, str] = {}
        if MCP_KEY_SECRET in wf.secrets:
            key = self._mcp_key()
            if not key:
                return {"error": f"{workflow!r} reaches the robot over MCP: set mcp_api_key in this service's config"}
            secrets[MCP_KEY_SECRET] = key

        run_id = uuid.uuid4().hex[:8]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run = _Run(run_id=run_id, workflow=wf.name, agent=agent, run_name=f"{stamp}-{wf.name}-{agent}-{run_id}",
                   started_at=_now(), secrets=list(secrets.values()))
        self._runs[run_id] = run
        agent_config: Dict[str, Any] = {}
        if model or self.config.model:
            agent_config["model"] = model or self.config.model
        if agent == "claude_code" and self.config.claude_cli:
            agent_config["cli"] = self.config.claude_cli
        run.task = asyncio.create_task(self._execute(run, filled, secrets, agent_config))
        logger.info("agent %s: started %s (%s) as %s", self.proxy_id, wf.name, agent, run_id)
        self._schedule_state()
        return {"run_id": run_id, "status": "running", "workflow": wf.name, "agent": agent}

    async def _execute(self, run: _Run, inputs: Dict[str, Any], secrets: Dict[str, str],
                       agent_config: Dict[str, Any]) -> None:
        from agent_harness import prune_workspaces, run_workflow
        runs_dir = self._workspace / "runs"

        def on_event(event) -> None:            # worker thread → service loop
            self._loop.call_soon_threadsafe(self._on_event, run, event)

        try:
            result = await asyncio.to_thread(
                run_workflow, run.workflow, run.agent, inputs,
                workspace_root=runs_dir,
                workflow_paths=self._workflow_paths(),
                skill_paths=self._skill_paths(),
                agents_md=self._agents_md(),
                agent_config=agent_config or None,
                policy={"residency": self.config.residency},
                secrets=secrets,
                run_name=run.run_name,
                on_event=on_event,
                cancel=run.cancel,
            )
            run.summary = self._summarize(run, result)
        except Exception as exc:  # noqa: BLE001 — run_workflow shouldn't raise; be safe
            logger.exception("agent %s: run %s crashed", self.proxy_id, run.run_id)
            run.summary = {"run_id": run.run_id, "workflow": run.workflow, "agent": run.agent,
                           "status": "error", "error": str(exc), "started_at": run.started_at, "ended_at": _now()}
        run.status = run.summary["status"]
        run.phase = "done"
        self.publish(f"runs/{run.run_id}/result", run.summary, retained=True)
        try:
            prune_workspaces(runs_dir, keep=self.config.keep_runs, match=f"*-{run.workflow}-*")
        except Exception:  # noqa: BLE001
            logger.debug("agent: prune failed", exc_info=True)
        self._announce(run)
        self._finished[run.run_id] = run.summary
        while len(self._finished) > 50:
            self._finished.popitem(last=False)
        self._runs.pop(run.run_id, None)
        await self._publish_state()

    def _summarize(self, run: _Run, result) -> Dict[str, Any]:
        deliverables: Dict[str, str] = {}
        for rel in result.deliverables or []:
            path = Path(result.workspace or "") / rel
            try:
                deliverables[rel] = path.read_text(encoding="utf-8")[:_DELIVERABLE_MAX]
            except OSError:
                pass
        return {
            "run_id": run.run_id, "workflow": run.workflow, "agent": run.agent,
            "status": result.status, "error": result.error,
            "checks_passed": result.checks_passed, "checks_total": result.checks_total,
            "cost_usd": result.cost_usd, "duration_s": result.duration_s, "model": result.model,
            "deliverables": deliverables, "workspace": result.workspace,
            "started_at": run.started_at, "ended_at": _now(),
        }

    def _on_event(self, run: _Run, event) -> None:
        run.events += 1
        data = event.data or {}
        if event.type in ("tool_call", "command", "file_change"):
            run.tool_calls += 1
            run.phase = "working"
            run.phase_detail = str(data.get("tool") or data.get("command") or data.get("path") or event.type)[:120]
        elif event.type == "message":
            run.phase = "thinking"
            run.last_message = str(data.get("text") or "")[:_EVENT_TEXT_MAX]
        elif event.type == "status" and data.get("phase"):
            run.phase = str(data["phase"])
        elif event.type == "error":
            run.last_message = str(data.get("message") or "")[:_EVENT_TEXT_MAX]
        record = {"type": event.type, "ts": event.ts, "data": data}
        text = json.dumps(record, default=str)
        for secret in run.secrets:
            text = text.replace(secret, "***")
        try:
            self.publish(f"runs/{run.run_id}/events", json.loads(text))
        except Exception:  # noqa: BLE001
            logger.debug("agent: event publish failed", exc_info=True)
        self._schedule_state()

    def _announce(self, run: _Run) -> None:
        """Speak a one-line outcome on ``announce_topic`` (if set)."""
        topic = (self.config.announce_topic or "").strip()
        if not topic or not run.summary:
            return
        s = run.summary
        if s["status"] == "completed":
            first = next((ln for text in s.get("deliverables", {}).values()
                          for ln in text.splitlines() if ln.strip()), "")
            text = f"The {run.workflow.replace('-', ' ')} run finished. {_plain(first)}"
        elif s["status"] == "cancelled":
            text = f"The {run.workflow.replace('-', ' ')} run was cancelled."
        else:
            text = f"The {run.workflow.replace('-', ' ')} run did not pass its checks." \
                if s["status"] == "failed_checks" else f"The {run.workflow.replace('-', ' ')} run failed."
        try:
            from botgear.runtime.bus import get_bus
            get_bus().publish_sync(topic, {"action": "speak", "text": text[:300]})
        except Exception:  # noqa: BLE001
            logger.debug("agent: announce failed", exc_info=True)

    @service_method("cancel_run")
    async def m_cancel_run(self, run_id: str) -> Dict[str, Any]:
        """Stop a running run (the agent process is terminated)."""
        run = self._runs.get(run_id)
        if run is None:
            return {"cancelled": False, "reason": f"no active run {run_id}"}
        run.cancel.set()
        return {"cancelled": True}

    @service_method("get_run")
    async def m_get_run(self, run_id: str) -> Dict[str, Any]:
        """A run's live status, or its final summary (status, checks, cost,
        deliverable text) once it has ended."""
        run = self._runs.get(run_id)
        if run is not None:
            return run.live()
        if run_id in self._finished:
            return dict(self._finished[run_id])
        found = self._read_result(run_id)
        return found if found is not None else {"error": f"no run {run_id}"}

    @service_method("wait_run")
    async def m_wait_run(self, run_id: str, timeout_seconds: float = 20.0) -> Dict[str, Any]:
        """Wait up to ``timeout_seconds`` (max 25) for a run to end, then
        return ``get_run``. Status ``running`` means it's still going."""
        run = self._runs.get(run_id)
        if run is not None and run.task is not None:
            await asyncio.wait({run.task}, timeout=max(0.0, min(float(timeout_seconds), 25.0)))
        return await self.m_get_run(run_id)

    @service_method("list_runs")
    async def m_list_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Active runs, then the most recent finished ones."""
        out = [r.live() for r in self._runs.values()]
        for summary in reversed(self._finished.values()):
            out.append({k: v for k, v in summary.items() if k != "deliverables"})
        active = {r["run_id"] for r in out}
        for d in sorted((self._workspace / "runs").glob("*"), key=lambda p: p.name, reverse=True):
            if len(out) >= limit:
                break
            run_id = d.name.rsplit("-", 1)[-1]
            if run_id in active:
                continue
            summary = self._read_result(run_id, d)
            if summary is not None:
                summary.pop("deliverables", None)
                out.append(summary)
        return out[:limit]

    @service_method("get_run_log")
    async def m_get_run_log(self, run_id: str, limit: int = 200) -> Dict[str, Any]:
        """The run's recent agent events and its check results, from its workspace."""
        d = self._run_dir(run_id)
        if d is None:
            return {"error": f"no run {run_id}"}
        events = []
        path = d / "events.jsonl"
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, min(int(limit), 2000)):]:
                try:
                    e = json.loads(line)
                    e.pop("raw", None)
                    events.append(e)
                except ValueError:
                    continue
        checks = []
        if (d / "checks.json").is_file():
            try:
                checks = json.loads((d / "checks.json").read_text(encoding="utf-8"))
            except ValueError:
                pass
        return {"run_id": run_id, "events": events, "checks": checks}

    def _run_dir(self, run_id: str) -> Optional[Path]:
        if not re.fullmatch(r"[0-9a-f]{8}", run_id or ""):
            return None
        return next((d for d in (self._workspace / "runs").glob(f"*-{run_id}") if d.is_dir()), None)

    def _read_result(self, run_id: str, d: Optional[Path] = None) -> Optional[Dict[str, Any]]:
        d = d or self._run_dir(run_id)
        if d is None or not (d / "result.json").is_file():
            return None
        try:
            r = json.loads((d / "result.json").read_text(encoding="utf-8"))
        except ValueError:
            return None
        deliverables = {}
        for rel in r.get("deliverables") or []:
            p = d / rel
            if p.is_file():
                deliverables[rel] = p.read_text(encoding="utf-8", errors="replace")[:_DELIVERABLE_MAX]
        return {"run_id": run_id, "workflow": r.get("workflow"), "agent": r.get("agent"),
                "status": r.get("status"), "error": r.get("error"),
                "checks_passed": r.get("checks_passed"), "checks_total": r.get("checks_total"),
                "cost_usd": r.get("cost_usd"), "duration_s": r.get("duration_s"), "model": r.get("model"),
                "deliverables": deliverables, "workspace": str(d), "ended_at": _mtime(d / "result.json")}

    # ─── state ───────────────────────────────────────────────────────

    def _snapshot(self) -> Dict[str, Any]:
        return {
            "default_agent": self.config.default_agent,
            "residency": self.config.residency,
            "mcp_key_set": bool(self._mcp_key()),
            "mcp_endpoint": self._mcp_endpoint(),
            "announce_topic": self.config.announce_topic,
            "halted": _halted(),
            "workflows": self._workflow_names(),
            "active_runs": [r.live() for r in self._runs.values()],
        }

    async def _publish_state(self) -> None:
        self._publish_pending = False
        self.publish("state", self._snapshot(), retained=True)

    def _schedule_state(self) -> None:
        """Coalesce bursts of run events into one state publish."""
        if self._publish_pending:
            return
        self._publish_pending = True
        self._loop.call_later(0.25, lambda: asyncio.ensure_future(self._publish_state()))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mtime(path: Path) -> Optional[str]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return None


def _plain(line: str) -> str:
    return re.sub(r"[#*_`>|]", "", line).strip()


def _halted() -> bool:
    try:
        from botgear.runtime import halt
    except Exception:  # noqa: BLE001
        return False
    return halt.is_halted()
