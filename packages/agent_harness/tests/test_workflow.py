from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.workflow import (WorkflowError, load_workflow, render_task, resolve_inputs,
                                    resolve_skills, substitute)


def test_load_workflow(content: Path):
    wf = load_workflow("summarize", [content / "workflows"])
    assert wf.skills == ["lister"]
    assert wf.outputs == ["output/summary.md"]
    assert wf.inputs["data_dir"].type == "path"
    assert wf.stages == ["01-gather", "02-write"]


def test_missing_workflow(content: Path):
    with pytest.raises(WorkflowError):
        load_workflow("nope", [content / "workflows"])


def test_output_outside_output_dir_rejected(content: Path):
    md = content / "workflows" / "summarize" / "WORKFLOW.md"
    md.write_text(md.read_text().replace("outputs: [output/summary.md]", "outputs: [summary.md]"))
    with pytest.raises(WorkflowError, match="under output/"):
        load_workflow("summarize", [content / "workflows"])


def test_resolve_inputs(content: Path, data_dir: Path):
    wf = load_workflow("summarize", [content / "workflows"])
    assert resolve_inputs(wf, {"data_dir": str(data_dir)})["data_dir"] == data_dir.resolve()
    with pytest.raises(WorkflowError, match="missing required"):
        resolve_inputs(wf, {})
    with pytest.raises(WorkflowError, match="unknown inputs"):
        resolve_inputs(wf, {"data_dir": str(data_dir), "extra": 1})
    with pytest.raises(WorkflowError, match="does not exist"):
        resolve_inputs(wf, {"data_dir": "/definitely/not/here"})


def test_resolve_skills(content: Path):
    assert resolve_skills(["lister"], [content / "skills"]) == {"lister": content / "skills" / "lister"}
    with pytest.raises(WorkflowError):
        resolve_skills(["missing"], [content / "skills"])


def test_substitute_keeps_unknown_braces():
    assert substitute("a {x} {y}", {"x": 1}) == "a 1 {y}"


def test_render_task(content: Path, data_dir: Path):
    wf = load_workflow("summarize", [content / "workflows"])
    inputs = resolve_inputs(wf, {"data_dir": str(data_dir)})
    text = render_task(wf, inputs, {"lister": Path("x")}, {"python": "py", "input.data_dir": "inputs/data_dir"}, tools=True)
    assert "Summarize inputs/data_dir" in text
    assert "Literal {braces} survive" in text
    assert "`inputs/data_dir` (read-only)" in text
    assert "`output/summary.md`" in text
    assert "01-gather" in text
    assert "`py .claude/skills/<skill>/scripts/<script>.py <args>`" in text
    assert "## Self-check" in text   # checks rendered for the agent to run itself


def test_deliverables_default_to_outputs_and_must_be_outputs(content: Path):
    wf = load_workflow("summarize", [content / "workflows"])
    assert wf.deliverables == wf.outputs
    md = content / "workflows" / "summarize" / "WORKFLOW.md"
    md.write_text(md.read_text().replace("outputs: [output/summary.md]",
                                         "outputs: [output/summary.md]\ndeliverables: [output/other.md]"))
    with pytest.raises(WorkflowError, match="not declared outputs"):
        load_workflow("summarize", [content / "workflows"])
