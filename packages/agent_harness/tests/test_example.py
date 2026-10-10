"""The README's worked example (examples/word-report) runs end to end, with a
scripted agent standing in for a real one: an honest agent passes the check,
one that misreports a count is caught and nothing is saved."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from agent_harness import AgentEvent, Capabilities, register_driver, run_skill, run_workflow, validate_skill_dir

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class ScriptedAgent:
    """Does what the task asks: runs the skill's script, writes both outputs.
    config["lie"] makes it report a wrong count."""
    name = "scripted"
    capabilities = Capabilities(tools=True, streaming=True, resume=False, cloud=False)

    def __init__(self, config):
        self.config = config

    def run(self, workspace, task, policy):
        script = workspace / ".claude/skills/count-words/scripts/count_words.py"
        counts = json.loads(subprocess.run([sys.executable, str(script), str(workspace / "inputs/docs")],
                                           capture_output=True, text=True, check=True).stdout)
        if self.config.get("lie"):
            counts["hello.txt"] += 1
        out = workspace / "output"
        (out / "counts.json").write_text(json.dumps(counts))
        lines = [f"- {name}: {n} words" for name, n in sorted(counts.items(), key=lambda kv: -kv[1])]
        (out / "report.md").write_text("\n".join(lines) + "\n")
        (out / "OUTPUT.md").write_text("\n".join(lines) + "\n")
        yield AgentEvent("completed", self.name, {"message": "done"})

    def cancel(self):
        pass


register_driver("scripted", ScriptedAgent)


def _workflow(tmp_path, **config):
    saved = []
    result = run_workflow("word-report", "scripted", {"docs": EXAMPLES / "docs"}, workspace_root=tmp_path,
                          workflow_paths=[EXAMPLES / "workflows"], skill_paths=[EXAMPLES / "skills"],
                          agent_config=config, on_success=lambda r: saved.append(r.outputs))
    return result, saved


def test_example_skill_is_valid():
    assert validate_skill_dir(EXAMPLES / "skills" / "count-words") == []


def test_example_workflow_passes_its_check(tmp_path):
    result, saved = _workflow(tmp_path)
    assert result.status == "completed" and (result.checks_passed, result.checks_total) == (3, 3)
    assert saved == [["output/counts.json", "output/report.md"]]
    ws = Path(result.workspace)
    assert json.loads((ws / "output/counts.json").read_text()) == {"fox.txt": 9, "hello.txt": 2}
    assert "## Self-check" in (ws / "task.md").read_text()          # the agent was told how to verify itself


def test_example_check_catches_a_wrong_count(tmp_path):
    result, saved = _workflow(tmp_path, lie=True)
    assert result.status == "failed_checks" and saved == []
    assert "FAIL: counts.json" in result.error


def test_example_skill_runs_alone(tmp_path):
    result = run_skill("count-words", "scripted", {"docs": EXAMPLES / "docs"}, workspace_root=tmp_path,
                       skill_paths=[EXAMPLES / "skills"])
    assert result.status == "completed" and (result.checks_passed, result.checks_total) == (1, 1)
    assert "## Self-check" not in (Path(result.workspace) / "task.md").read_text()
