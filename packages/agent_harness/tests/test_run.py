from __future__ import annotations

import json
from pathlib import Path

from agent_harness.run import run_workflow


def _run(content: Path, data_dir: Path, tmp_path: Path, **kwargs):
    defaults = dict(workspace_root=tmp_path / "runs", workflow_paths=[content / "workflows"],
                    skill_paths=[content / "skills"], agents_md="# rules\n", run_name="r1")
    defaults.update(kwargs)
    return run_workflow("summarize", "fake", {"data_dir": str(data_dir)}, **defaults)


def test_completed_run_builds_workspace(content: Path, data_dir: Path, tmp_path: Path):
    events = []
    result = _run(content, data_dir, tmp_path, on_event=events.append,
                  agent_config={"files": {"output/summary.md": "ok summary", "output/01-gather/notes.md": "n"}})
    ws = Path(result.workspace)
    assert result.status == "completed", result.error
    assert result.checks_passed == result.checks_total == 2
    assert result.model == "fake-1" and result.session_id == "s-1"
    assert (ws / "inputs" / "data_dir").is_symlink()
    assert (ws / "inputs" / "data_dir" / "a.txt").read_text() == "hello"
    assert (ws / ".claude/skills/lister/SKILL.md").is_file()
    assert (ws / ".agents/skills/lister/scripts/ls.py").is_file()
    assert (ws / "AGENTS.md").read_text() == "# rules\n"
    assert (ws / ".git").is_dir()
    assert (ws / "workflow" / "WORKFLOW.md").is_file()  # workspace is its own project boundary
    assert "Summarize inputs/data_dir" in (ws / "task.md").read_text()
    state = json.loads((ws / "state.json").read_text())
    assert state["status"] == "completed"
    assert state["completed_stages"] == ["01-gather"]
    assert "output/summary.md" in state["artifacts"]
    assert len((ws / "events.jsonl").read_text().splitlines()) == len(events) == 5
    assert json.loads((ws / "result.json").read_text())["status"] == "completed"


def test_failed_checks(content: Path, data_dir: Path, tmp_path: Path):
    result = _run(content, data_dir, tmp_path, agent_config={"files": {"output/summary.md": "nope"}})
    assert result.status == "failed_checks"
    assert result.checks_passed == 1 and result.checks_total == 2
    checks = json.loads((Path(result.workspace) / "checks.json").read_text())
    assert [c["passed"] for c in checks] == [True, False]


def test_missing_output(content: Path, data_dir: Path, tmp_path: Path):
    result = _run(content, data_dir, tmp_path, agent_config={"files": {}})
    assert result.status == "failed_checks"
    assert "output/summary.md is missing or empty" in result.error


def test_agent_error(content: Path, data_dir: Path, tmp_path: Path):
    result = _run(content, data_dir, tmp_path, agent_config={"fail": True})
    assert result.status == "error" and result.error == "boom"
    assert json.loads((Path(result.workspace) / "state.json").read_text())["status"] == "error"


def test_residency_gate_refuses_cloud_agent(content: Path, data_dir: Path, tmp_path: Path):
    result = _run(content, data_dir, tmp_path, agent_config={"cloud": True})
    assert result.status == "error" and "local_only" in result.error
    assert result.workspace is None  # refused before any workspace/data staging


def test_residency_cloud_ok(content: Path, data_dir: Path, tmp_path: Path):
    result = _run(content, data_dir, tmp_path, policy={"residency": "cloud_ok"},
                  agent_config={"cloud": True, "files": {"output/summary.md": "ok"}})
    assert result.status == "completed", result.error


def test_bad_input_is_error_result(content: Path, tmp_path: Path):
    result = run_workflow("summarize", "fake", {}, workspace_root=tmp_path, workflow_paths=[content / "workflows"],
                          skill_paths=[content / "skills"])
    assert result.status == "error" and "missing required input" in result.error
