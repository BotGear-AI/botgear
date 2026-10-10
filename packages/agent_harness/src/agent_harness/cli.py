"""agent-harness CLI.

  agent-harness run --workflow interpret-report --agent claude_code \\
      --input run_dir=/path/to/run --workspace-root /tmp/agent_runs \\
      --workflow-path ./content/workflows --skill-path ./content/skills \\
      [--agents-md ./content/AGENTS.md] [--config model=sonnet] [--policy residency=cloud_ok]

  agent-harness drivers          list available agents

Prints the RunResult as JSON on stdout; a one-line summary of each agent
event goes to stderr as it happens.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agent_harness.events import AgentEvent
from agent_harness.run import run_workflow
from agent_harness.runtime import available_drivers


def _pairs(values: list[str], flag: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in values or []:
        if "=" not in item:
            raise SystemExit(f"{flag} expects key=value, got {item!r}")
        key, value = item.split("=", 1)
        try:
            out[key] = json.loads(value)   # numbers/bools/lists; plain strings fall through
        except json.JSONDecodeError:
            out[key] = value
    return out


def _print_event(event: AgentEvent) -> None:
    d = event.data
    summary = {
        "message": lambda: d.get("text", "")[:200].replace("\n", " "),
        "command": lambda: d.get("command", ""),
        "file_change": lambda: f"{d.get('tool')} {d.get('path')}",
        "tool_call": lambda: f"{d.get('tool')} {json.dumps(d.get('input'))[:150]}",
        "tool_result": lambda: ("ERROR " if d.get("is_error") else "") + d.get("content", "")[:120].replace("\n", " "),
        "permission_request": lambda: f"denied {d.get('tool') or ''} {json.dumps(d.get('input')) if d.get('input') else ''}",
        "usage": lambda: json.dumps({k: d.get(k) for k in ("total_cost_usd", "num_turns", "input_tokens", "output_tokens") if d.get(k) is not None}),
        "status": lambda: d.get("phase") or d.get("text", ""),
        "error": lambda: d.get("message", ""),
        "completed": lambda: d.get("message", "")[:200].replace("\n", " "),
    }[event.type]()
    print(f"[{event.type}] {summary}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-harness", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("drivers", help="list available agents")

    run = sub.add_parser("run", help="run one workflow with one agent")
    run.add_argument("--workflow", required=True)
    run.add_argument("--agent", required=True)
    run.add_argument("--input", action="append", default=[], help="key=value (repeatable)")
    run.add_argument("--workspace-root", required=True, type=Path)
    run.add_argument("--workflow-path", action="append", default=[], type=Path, required=True)
    run.add_argument("--skill-path", action="append", default=[], type=Path)
    run.add_argument("--agents-md", type=Path, default=None)
    run.add_argument("--config", action="append", default=[], help="driver config key=value (repeatable)")
    run.add_argument("--policy", action="append", default=[], help="policy override key=value (repeatable)")
    run.add_argument("--python", default=sys.executable, help="interpreter for skill scripts")
    run.add_argument("--quiet", action="store_true", help="don't stream events to stderr")

    args = parser.parse_args(argv)
    if args.cmd == "drivers":
        print("\n".join(available_drivers()))
        return 0

    agents_md = args.agents_md.read_text(encoding="utf-8") if args.agents_md else ""
    result = run_workflow(
        args.workflow, args.agent, _pairs(args.input, "--input"),
        workspace_root=args.workspace_root,
        workflow_paths=args.workflow_path,
        skill_paths=args.skill_path,
        agents_md=agents_md,
        agent_config=_pairs(args.config, "--config"),
        policy=_pairs(args.policy, "--policy"),
        python=args.python,
        on_event=None if args.quiet else _print_event,
    )
    print(json.dumps(asdict(result), indent=2, default=str))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
