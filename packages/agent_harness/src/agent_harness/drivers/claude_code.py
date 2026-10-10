"""claude_code driver: runs the Claude Code agent headless (`claude -p`).

We rent Claude Code's whole agent loop (planning, tools, context
management) and constrain it from outside:

  --permission-mode dontAsk       anything not explicitly allowed is denied
                                  (never --dangerously-skip-permissions)
  --setting-sources project       ignore the host user's settings (their
                                  permission mode, hooks, MCP servers)
  --strict-mcp-config             no MCP servers unless we pass them:
  --mcp-config <inline JSON>      exactly the workflow's `mcp:` servers (only
                                  with policy mcp=allow), headers holding
                                  `${SECRET}` references that the CLI expands
                                  from the process env, so the value is never
                                  on the command line; each server's tools
                                  allowlisted as mcp__<server>__<tool>
                                  (`["*"]` → mcp__<server>, all its tools)
  --tools / --allowedTools        Read/Grep/Glob anywhere readable; Bash ONLY
                                  for skill scripts via the interpreter and
                                  the workflow's own check commands, exactly
                                  as rendered (self-check);
                                  Write/Edit ONLY under ./output/
  --add-dir <input dirs>          inputs are symlinks; their real dirs must be
                                  readable
  CLAUDE_CODE_DISABLE_CLAUDE_MDS  don't pull CLAUDE.md/AGENTS.md from parent
  CLAUDE_CODE_DISABLE_AUTO_MEMORY dirs or the user's memory; the workspace's
  CLAUDE_CODE_DISABLE_BUNDLED_SKILLS  AGENTS.md is passed explicitly instead

Known behaviour: Claude Code auto-approves a built-in set of READ-ONLY shell
commands (e.g. `ls`, `echo`) even in dontAsk mode, so Bash is "skill scripts
+ read-only commands", not strictly skill scripts. Writes still only reach
./output/ (Write/Edit rules); anything else that writes is denied.

Network: denied unless the policy says network="allow", which enables
WebSearch + WebFetch (WebFetch narrowed to policy.allowed_domains if set).

Config (all optional): cli, model, max_turns, api_key, extra_args (list),
extra_allowed_tools (list), env (dict). Secrets arrive via task.env.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from agent_harness.events import AgentEvent
from agent_harness.mcp_servers import env_ref_headers
from agent_harness.policy import Policy
from agent_harness.runtime import AgentTask, Capabilities

_ISOLATION_ENV = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
}
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


class ClaudeCodeDriver:
    name = "claude_code"
    capabilities = Capabilities(tools=True, streaming=True, resume=True, cloud=True, mcp=True, skills=True)
    kill_grace_s = 10.0       # after SIGTERM on timeout/cancel, SIGKILL if still running

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._proc: Optional[subprocess.Popen] = None

    def build_command(self, task: AgentTask, policy: Policy) -> list[str]:
        cfg = self.config
        allowed = ["Read", "Grep", "Glob", f"Bash({task.python} .claude/skills/*)",
                   f"Bash({task.python} workflow/checks/*)",
                   *(f"Bash({cmd})" for cmd in task.self_checks),
                   "Write(./output/**)", "Edit(./output/**)", *cfg.get("extra_allowed_tools", [])]
        tools = ["Read", "Grep", "Glob", "Bash", "Write", "Edit"]
        if policy.network == "allow":
            # Web access only when the workflow's policy opts in. A domain
            # allowlist narrows WebFetch; WebSearch can't be domain-limited.
            tools += ["WebSearch", "WebFetch"]
            allowed.append("WebSearch")
            if policy.allowed_domains:
                allowed += [f"WebFetch(domain:{d})" for d in policy.allowed_domains]
            else:
                allowed.append("WebFetch")
        mcp_config = None
        if task.mcp_servers and policy.mcp == "allow":
            mcp_config = {"mcpServers": {
                s.name: {"type": "http", "url": s.url, **({"headers": env_ref_headers(s)} if s.headers else {})}
                for s in task.mcp_servers
            }}
            for s in task.mcp_servers:
                allowed += [f"mcp__{s.name}"] if s.all_tools else [f"mcp__{s.name}__{t}" for t in s.tools]
        cmd = [
            cfg.get("cli", "claude"), "-p", task.prompt,
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", "dontAsk",
            "--setting-sources", "project",
            "--strict-mcp-config",
            *(["--mcp-config", json.dumps(mcp_config)] if mcp_config else []),
            "--tools", ",".join(tools),
            "--allowedTools", *allowed,
        ]
        if task.system_context:
            cmd += ["--append-system-prompt", task.system_context]
        if cfg.get("model"):
            cmd += ["--model", cfg["model"]]
        max_turns = cfg.get("max_turns") or policy.max_turns
        if max_turns:
            cmd += ["--max-turns", str(max_turns)]
        for read_dir in task.read_dirs:
            cmd += ["--add-dir", str(read_dir)]
        cmd += list(cfg.get("extra_args", []))
        return cmd

    def run(self, workspace: Path, task: AgentTask, policy: Policy) -> Iterator[AgentEvent]:
        cmd = self.build_command(task, policy)
        env = {**os.environ, **_ISOLATION_ENV, **self.config.get("env", {}), **task.env}
        ca = _mcp_ca_bundle(workspace, task)
        if ca:
            # Node (the CLI) trusts extra CAs only via this var, read at start.
            env["NODE_EXTRA_CA_CERTS"] = ca
        if self.config.get("api_key"):
            # Headless servers authenticate with an API key; without one the
            # CLI falls back to its own login (fine for a dev machine).
            env["ANTHROPIC_API_KEY"] = str(self.config["api_key"])
        yield AgentEvent("status", self.name, {"phase": "starting", "command": _redact(cmd)})
        try:
            self._proc = subprocess.Popen(cmd, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as exc:
            yield AgentEvent("error", self.name, {"message": f"could not start {cmd[0]}: {exc}", "fatal": True})
            return

        # Drain stderr concurrently: a CLI that writes more than a pipe buffer
        # there would otherwise block, stall stdout, and deadlock this loop.
        stderr_tail: list[str] = []
        drain = threading.Thread(target=_drain, args=(self._proc.stderr, stderr_tail), daemon=True)
        drain.start()
        deadline = time.time() + policy.max_minutes * 60
        timer = threading.Timer(max(1.0, deadline - time.time()), self.cancel)
        timer.daemon = True
        timer.start()
        saw_result = False
        try:
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    yield AgentEvent("status", self.name, {"text": line})
                    continue
                for event in self._translate(raw):
                    saw_result = saw_result or event.type == "completed"
                    yield event
            self._proc.wait()
        finally:
            timer.cancel()

        drain.join(timeout=5)
        stderr = "".join(stderr_tail).strip()
        if time.time() >= deadline:
            yield AgentEvent("error", self.name, {"message": f"timed out after {policy.max_minutes} min", "fatal": True})
        elif self._proc.returncode != 0 or not saw_result:
            yield AgentEvent("error", self.name, {
                "message": f"claude exited {self._proc.returncode}: {stderr[-1000:]}", "fatal": True})

    def cancel(self) -> None:
        """SIGTERM, then SIGKILL after kill_grace_s if the process ignores it
        (otherwise its open stdout would keep the run from ever returning)."""
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            killer = threading.Timer(self.kill_grace_s, lambda: proc.poll() is None and proc.kill())
            killer.daemon = True
            killer.start()

    def _translate(self, raw: dict[str, Any]) -> Iterator[AgentEvent]:
        kind = raw.get("type")
        if kind == "system":
            subtype = raw.get("subtype")
            if subtype == "init":
                yield AgentEvent("status", self.name, {
                    "phase": "init", "model": raw.get("model"), "session_id": raw.get("session_id"),
                    "tools": raw.get("tools"), "skills": raw.get("skills"),
                    "permission_mode": raw.get("permissionMode")}, raw)
            elif subtype == "permission_denied":
                yield AgentEvent("permission_request", self.name, {"denied": True}, raw)
            return
        if kind == "assistant":
            for block in (raw.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    yield AgentEvent("message", self.name, {"text": block["text"]}, raw)
                elif block.get("type") == "tool_use":
                    name, args = block.get("name"), block.get("input") or {}
                    if name == "Bash":
                        yield AgentEvent("command", self.name, {"command": args.get("command"), "id": block.get("id")}, raw)
                    elif name in _WRITE_TOOLS:
                        yield AgentEvent("file_change", self.name, {"tool": name, "path": args.get("file_path"), "id": block.get("id")}, raw)
                    else:
                        yield AgentEvent("tool_call", self.name, {"tool": name, "input": args, "id": block.get("id")}, raw)
            return
        if kind == "user":
            content = (raw.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result":
                    text = block.get("content")
                    if isinstance(text, list):
                        text = " ".join(str(part.get("text", "")) for part in text if isinstance(part, dict))
                    yield AgentEvent("tool_result", self.name, {
                        "id": block.get("tool_use_id"), "is_error": bool(block.get("is_error")),
                        "content": str(text)[:2000]}, raw)
            return
        if kind == "result":
            yield AgentEvent("usage", self.name, {
                "total_cost_usd": raw.get("total_cost_usd"), "num_turns": raw.get("num_turns"),
                "duration_ms": raw.get("duration_ms"), **(raw.get("usage") or {})}, raw)
            for denial in raw.get("permission_denials") or []:
                yield AgentEvent("permission_request", self.name, {"denied": True, "tool": denial.get("tool_name"),
                                                                   "input": denial.get("tool_input")})
            if raw.get("is_error"):
                yield AgentEvent("error", self.name, {"message": raw.get("result") or raw.get("subtype"),
                                                      "fatal": True}, raw)
            yield AgentEvent("completed", self.name, {"message": raw.get("result") or "",
                                                      "subtype": raw.get("subtype")}, raw)


def _mcp_ca_bundle(workspace: Path, task: AgentTask) -> Optional[str]:
    """Path for NODE_EXTRA_CA_CERTS covering every MCP server's ca_cert
    (one file as-is; several concatenated into the workspace)."""
    certs = sorted({s.ca_cert for s in task.mcp_servers if s.ca_cert})
    if not certs:
        return None
    if len(certs) == 1:
        return certs[0]
    bundle = Path(workspace) / ".mcp-ca.pem"
    bundle.write_text("".join(Path(c).read_text(encoding="utf-8").rstrip() + "\n" for c in certs),
                      encoding="utf-8")
    return str(bundle)


def _drain(stream: Any, tail: list[str], keep: int = 20_000) -> None:
    """Read a stream to EOF, keeping only its last `keep` characters."""
    if stream is None:
        return
    size = 0
    for chunk in iter(lambda: stream.read(4096), ""):
        tail.append(chunk)
        size += len(chunk)
        while size > keep and len(tail) > 1:
            size -= len(tail.pop(0))


def _redact(cmd: list[str]) -> list[str]:
    """Log the command without the (long) prompt and system context."""
    out, skip = [], False
    for part in cmd:
        if skip:
            out.append(f"<{len(part)} chars>")
            skip = False
            continue
        out.append(part)
        skip = part in ("-p", "--append-system-prompt")
    return out
