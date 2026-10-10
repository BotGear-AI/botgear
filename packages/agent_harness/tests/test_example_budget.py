"""The budget-check example (examples/workflows/budget-check) end to end, with
scripted agents in place of real ones: a plain-value input, a deliverables
subset, a check that judges the DECISION as well as the numbers, and the
tool-less path (prompt_inject's own prompt builder + context_commands)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from agent_harness import AgentEvent, Capabilities, register_driver, run_workflow, validate_skill_dir
from agent_harness.drivers.prompt_inject import PromptInjectDriver, parse_files

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _write_answer(workspace: Path, totals: dict, budget: float, flip: bool = False) -> None:
    over = totals["total"] > budget
    if flip:
        over = not over
    largest = max(totals["by_category"], key=totals["by_category"].get)
    (workspace / "output/totals.json").write_text(json.dumps(totals))
    (workspace / "output/verdict.md").write_text(
        f"{'OVER BUDGET' if over else 'WITHIN BUDGET'}\n\nSpent {totals['total']} against {budget}; "
        f"largest category: {largest}.\n")


class ToolAgent:
    """Runs the skill's script like a tool-using agent would. config: flip
    (wrong verdict), skew (wrong total)."""
    name = "budget_tools"
    capabilities = Capabilities(tools=True, streaming=True, resume=False, cloud=False)

    def __init__(self, config):
        self.config = config

    def run(self, workspace, task, policy):
        script = workspace / ".claude/skills/sum-by-category/scripts/sum_by_category.py"
        totals = json.loads(subprocess.run([sys.executable, str(script), str(workspace / "inputs/expenses")],
                                           capture_output=True, text=True, check=True).stdout)
        budget = float(re.search(r"budget \(([\d.]+)\)", task.prompt).group(1))   # rendered from {input.budget}
        if self.config.get("skew"):
            totals["total"] += 10
        _write_answer(workspace, totals, budget, flip=self.config.get("flip", False))
        yield AgentEvent("completed", self.name, {"message": "done"})

    def cancel(self):
        pass


class NoToolsAgent(PromptInjectDriver):
    """prompt_inject without a model: builds the real prompt (which runs the
    workflow's context_commands), then 'answers' from what's in it."""
    name = "budget_no_tools"

    def run(self, workspace, task, policy):
        messages = self.build_messages(workspace, task)
        user = messages[1]["content"]
        totals = json.loads(user[user.index("{", user.index("## Totals")):user.rindex("}") + 1])
        budget = float(re.search(r"budget \(([\d.]+)\)", task.prompt).group(1))
        answer = {"output/totals.json": totals,
                  "output/verdict.md": f"{'OVER BUDGET' if totals['total'] > budget else 'WITHIN BUDGET'}\n\nok\n"}
        for rel, value in parse_files(json.dumps(answer), task.outputs).items():
            (workspace / rel).write_text(value if isinstance(value, str) else json.dumps(value))
        yield AgentEvent("completed", self.name, {"message": "done"})


register_driver("budget_tools", ToolAgent)
register_driver("budget_no_tools", lambda cfg: NoToolsAgent({"base_url": "http://localhost:1/v1", **cfg}))


def _run(tmp_path, agent="budget_tools", budget="400", **config):
    saved = []
    result = run_workflow("budget-check", agent,
                          {"expenses": EXAMPLES / "expenses" / "expenses.csv", "budget": budget},
                          workspace_root=tmp_path, workflow_paths=[EXAMPLES / "workflows"],
                          skill_paths=[EXAMPLES / "skills"], agent_config=config,
                          on_success=lambda r: saved.append(r.deliverables) or "saved")
    return result, saved


def test_skill_is_valid():
    assert validate_skill_dir(EXAMPLES / "skills" / "sum-by-category") == []


def test_within_budget(tmp_path):
    result, saved = _run(tmp_path, budget="400")
    assert result.status == "completed", result.error
    assert saved == [["output/verdict.md"]]                   # only the deliverable, not the evidence
    ws = Path(result.workspace)
    assert (ws / "output/verdict.md").read_text().startswith("WITHIN BUDGET")
    assert "compare the total\nwith the budget (400)" in (ws / "task.md").read_text()   # string input rendered


def test_over_budget(tmp_path):
    result, _ = _run(tmp_path, budget="300")
    assert result.status == "completed"
    assert (Path(result.workspace) / "output/verdict.md").read_text().startswith("OVER BUDGET")


def test_check_catches_a_wrong_verdict(tmp_path):
    result, saved = _run(tmp_path, budget="300", flip=True)
    assert result.status == "failed_checks" and saved == []
    assert "is 'OVER BUDGET'" in result.error


def test_check_catches_a_wrong_total(tmp_path):
    result, saved = _run(tmp_path, skew=True)
    assert result.status == "failed_checks" and saved == []
    assert "totals.json total 379.99 != actual 369.99" in result.error


def test_tool_less_agent_gets_the_totals_from_context_commands(tmp_path):
    result, saved = _run(tmp_path, agent="budget_no_tools", budget="300")
    assert result.status == "completed", result.error
    assert saved == [["output/verdict.md"]]
    assert json.loads((Path(result.workspace) / "output/totals.json").read_text())["total"] == 369.99
