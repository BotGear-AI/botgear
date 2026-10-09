"""Run the robot-health workflow against the live robot (roadmap Phase 2
proof; Phase 3's agent service will run workflows like this from the bus).

    BOTGEAR_MCP_KEY=<api-client secret> \\
      .venv/bin/python repo/agent/1.0.0/run_robot_health.py --cloud-ok

A Claude Code agent reads the robot through the MCP gateway (read-only
tools only), writes output/facts.json + output/report.md, and the workflow's
check re-reads the robot independently: only if every claim matches is the
run ``completed``. ``--cloud-ok`` is required for cloud agents because the
robot's state goes to the model (the harness defaults to local_only).
Runs land in data/agent_runs/ (last 10 kept).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from agent_harness import prune_workspaces, run_workflow

APP = Path(__file__).resolve().parents[3]          # apps/botgear
CONTENT = Path(__file__).resolve().parent / "content"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", default="claude_code")
    ap.add_argument("--endpoint", default="https://localhost:8998/v1/mcp")
    ap.add_argument("--ca", default=str(APP / "ssl" / "cert.pem"))
    ap.add_argument("--model", default=None, help="agent model (default: the agent's own)")
    ap.add_argument("--cloud-ok", action="store_true", help="allow a cloud agent to see the robot's state")
    args = ap.parse_args(argv)

    key = os.environ.get("BOTGEAR_MCP_KEY")
    if not key:
        print("set BOTGEAR_MCP_KEY to an API client's secret (UI: Tools → API Clients)", file=sys.stderr)
        return 2

    runs = APP / "data" / "agent_runs"
    result = run_workflow(
        "robot-health", args.agent, {"endpoint": args.endpoint, "ca_cert": args.ca},
        workspace_root=runs,
        workflow_paths=[CONTENT / "workflows"],
        skill_paths=[CONTENT / "skills"],
        agents_md=(CONTENT / "AGENT_CONTEXT.md").read_text(encoding="utf-8"),
        agent_config={"model": args.model} if args.model else None,
        policy={"residency": "cloud_ok" if args.cloud_ok else "local_only"},
        secrets={"BOTGEAR_MCP_KEY": key},
        on_event=lambda e: print(f"  [{e.type}] {str(e.data)[:140]}") if e.type in ("tool_call", "error") else None,
    )
    prune_workspaces(runs, keep=10, match="*-robot-health-*")

    print(f"\nstatus: {result.status}   checks {result.checks_passed}/{result.checks_total}   "
          f"{result.duration_s}s   ${result.cost_usd or 0:.3f}")
    if result.error:
        print(f"error: {result.error}")
    if result.workspace:
        report = Path(result.workspace) / "output" / "report.md"
        print(f"workspace: {result.workspace}")
        if report.is_file():
            print("\n" + report.read_text(encoding="utf-8"))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
