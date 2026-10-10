"""Run the word-report example: python examples/run_example.py [agent]

agent: claude_code (needs the `claude` CLI, logged in or ANTHROPIC_API_KEY set),
or any driver you registered. Workspaces go to examples/runs/.
"""
import sys
from pathlib import Path

from agent_harness import run_skill, run_workflow

HERE = Path(__file__).parent
agent = sys.argv[1] if len(sys.argv) > 1 else "claude_code"

# 1) The workflow: declared inputs, two required outputs, an external check.
result = run_workflow(
    "word-report", agent, {"docs": HERE / "docs"},
    workspace_root=HERE / "runs",
    workflow_paths=[HERE / "workflows"],
    skill_paths=[HERE / "skills"],
    policy={"residency": "cloud_ok"},            # allow a cloud agent for this demo data
    on_success=lambda r: print("checks passed; saving", r.outputs),
)
print(result.status, f"{result.checks_passed}/{result.checks_total} checks", result.workspace)

# 2) The same skill on its own: no WORKFLOW.md, no check beyond "OUTPUT.md exists".
quick = run_skill(
    "count-words", agent, {"docs": HERE / "docs"},
    workspace_root=HERE / "runs",
    skill_paths=[HERE / "skills"],
    instructions="Count the words in inputs/docs and write the table to output/OUTPUT.md.",
    policy={"residency": "cloud_ok"},
)
print(quick.status, quick.workspace)
