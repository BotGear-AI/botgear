"""Run the budget-check example: python examples/run_budget_check.py [agent] [budget]

agent: claude_code (default; needs the `claude` CLI, logged in or ANTHROPIC_API_KEY set)
       or prompt_inject (one model call, no tools; set AGENT_BASE_URL / AGENT_API_KEY /
       AGENT_MODEL, e.g. a local Ollama at http://localhost:11434/v1).
budget: default 400 (the sample data totals 369.99, so try 300 to see OVER BUDGET).
"""
import json
import os
import sys
from pathlib import Path

from agent_harness import run_workflow

HERE = Path(__file__).parent
agent = sys.argv[1] if len(sys.argv) > 1 else "claude_code"
budget = sys.argv[2] if len(sys.argv) > 2 else "400"

config = {}
policy = {"residency": "cloud_ok"}                 # allow a cloud agent for this demo data
if agent == "prompt_inject":
    config = {"base_url": os.environ.get("AGENT_BASE_URL", "http://localhost:11434/v1"),
              "api_key": os.environ.get("AGENT_API_KEY"), "model": os.environ.get("AGENT_MODEL", "llama3.1")}
    policy = {}                                    # a local endpoint passes the default local_only gate


def save_verdict(result):
    """The write-back: runs only if the check passed, so only a verified verdict is recorded."""
    verdict = (Path(result.workspace) / "output/verdict.md").read_text().splitlines()[0]
    ledger = HERE / "runs" / "ledger.json"
    entries = json.loads(ledger.read_text()) if ledger.exists() else []
    entries.append({"budget": budget, "verdict": verdict, "run": Path(result.workspace).name})
    ledger.write_text(json.dumps(entries, indent=2))
    return verdict


result = run_workflow(
    "budget-check", agent, {"expenses": HERE / "expenses" / "expenses.csv", "budget": budget},
    workspace_root=HERE / "runs",
    workflow_paths=[HERE / "workflows"],
    skill_paths=[HERE / "skills"],
    agent_config=config,
    policy=policy,
    on_success=save_verdict,
)
print(result.status, f"{result.checks_passed}/{result.checks_total} checks", "->", result.handled or result.error)
print("deliverables:", result.deliverables, "in", result.workspace)
