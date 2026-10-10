"""run_skill, secrets + redaction, on_success gating, partial status,
network policy, skills helpers, pricing, workspace pruning."""
from __future__ import annotations

import json
from pathlib import Path

from agent_harness import estimate_cost, list_skills, prune_workspaces, run_skill, validate_skill_dir
from agent_harness.drivers.claude_code import ClaudeCodeDriver
from agent_harness.policy import Policy
from agent_harness.run import run_workflow
from agent_harness.runtime import AgentTask
from agent_harness.skills import import_skill_script


def _wf_run(content, data_dir, tmp_path, **kw):
    return run_workflow("summarize", "fake", {"data_dir": str(data_dir)}, workspace_root=tmp_path / "runs",
                        workflow_paths=[content / "workflows"], skill_paths=[content / "skills"], **kw)


def test_run_skill_without_workflow(content: Path, data_dir: Path, tmp_path: Path):
    params = tmp_path / "params.json"
    params.write_text("{}")
    result = run_skill("lister", "fake", {"data": data_dir, "params.json": params, "mode": "fast"},
                       workspace_root=tmp_path / "runs", skill_paths=[content / "skills"],
                       agent_config={"files": {"output/OUTPUT.md": "done"}})
    assert result.status == "completed", result.error
    ws = Path(result.workspace)
    assert (ws / "inputs" / "data" / "a.txt").read_text() == "hello"
    assert (ws / "inputs" / "params.json").is_file()
    assert not (ws / "workflow").exists()          # no WORKFLOW.md to snapshot
    task = (ws / "task.md").read_text()
    assert "Run the `lister` skill" in task and "`mode`: `fast`" in task
    assert result.deliverables == ["output/OUTPUT.md"]


def test_run_skill_unknown_skill(content: Path, tmp_path: Path):
    result = run_skill("nope", "fake", {}, workspace_root=tmp_path, skill_paths=[content / "skills"])
    assert result.status == "error" and "nope" in result.error


def test_secrets_reach_agent_and_are_redacted(content: Path, data_dir: Path, tmp_path: Path):
    params = {"echo_env": True, "files": {"output/OUTPUT.md": "ok"}}
    result = run_skill("lister", "fake", {"data": data_dir}, workspace_root=tmp_path / "runs",
                       skill_paths=[content / "skills"], agent_config=params,
                       secrets={"MAPS_KEY": "s3cr3t-value"})
    events = (Path(result.workspace) / "events.jsonl").read_text()
    assert "MAPS_KEY=***" in events and "s3cr3t-value" not in events


def test_undeclared_secret_rejected(content: Path, data_dir: Path, tmp_path: Path):
    result = _wf_run(content, data_dir, tmp_path, secrets={"X": "y"},
                     agent_config={"files": {"output/summary.md": "ok"}})
    assert result.status == "error" and "not declared" in result.error


def test_on_success_only_after_checks_pass(content: Path, data_dir: Path, tmp_path: Path):
    calls = []
    good = _wf_run(content, data_dir, tmp_path, run_name="good", on_success=lambda r: calls.append(r.status) or "saved",
                   agent_config={"files": {"output/summary.md": "ok"}})
    assert good.handled == "saved" and calls == ["completed"]
    bad = _wf_run(content, data_dir, tmp_path, run_name="bad", on_success=lambda r: calls.append("bad"),
                  agent_config={"files": {"output/summary.md": "nope"}})
    assert bad.status == "failed_checks" and bad.handled is None and calls == ["completed"]


def test_partial_when_agent_dies_after_writing(content: Path, data_dir: Path, tmp_path: Path):
    result = _wf_run(content, data_dir, tmp_path, agent_config={"fail": True, "files": {"output/summary.md": "ok"}})
    assert result.status == "partial" and result.outputs == ["output/summary.md"] and result.error == "timed out"


def test_network_policy_controls_web_tools():
    task = AgentTask(prompt="p", outputs=[], python="py")
    closed = ClaudeCodeDriver({}).build_command(task, Policy())
    assert "WebSearch" not in " ".join(closed)
    opened = ClaudeCodeDriver({}).build_command(task, Policy(network="allow"))
    assert "Read,Grep,Glob,Bash,Write,Edit,WebSearch,WebFetch" in opened and "WebFetch" in opened
    narrow = ClaudeCodeDriver({}).build_command(task, Policy(network="allow", allowed_domains=["census.gov"]))
    assert "WebFetch(domain:census.gov)" in narrow and "WebFetch" not in narrow[narrow.index("--allowedTools"):]


def test_skill_validation_and_listing(tmp_path: Path):
    lib, user = tmp_path / "lib", tmp_path / "user"
    for base, desc in ((lib, "library copy"), (user, "user copy")):
        (base / "comp-check").mkdir(parents=True)
        (base / "comp-check" / "SKILL.md").write_text(f"---\nname: comp-check\ndescription: {desc}\n---\nbody\n")
    (lib / "_tools").mkdir()
    (lib / "Bad_Name").mkdir()
    (lib / "Bad_Name" / "SKILL.md").write_text("---\nname: Bad_Name\ndescription: x\n---\n")
    skills = {s["name"]: s for s in list_skills([user, lib])}
    assert skills["comp-check"]["description"] == "user copy"          # first path wins (fork)
    assert skills["comp-check"]["shadows"] == [str(lib)]
    assert "_tools" not in skills
    assert validate_skill_dir(lib / "comp-check") == []
    assert any("kebab-case" in e for e in validate_skill_dir(lib / "Bad_Name"))


def test_import_skill_script(content: Path):
    (content / "skills" / "lister" / "scripts" / "lib.py").write_text("def double(x):\n    return 2 * x\n")
    assert import_skill_script(content / "skills" / "lister", "scripts/lib.py").double(4) == 8


def test_pricing():
    assert estimate_cost("m", {"total_cost_usd": 0.42}) == 0.42
    assert estimate_cost("claude-haiku-4-5-20251001", {"input_tokens": 1_000_000, "output_tokens": 0}) == 1.0
    assert estimate_cost("unknown-model", {"input_tokens": 10}) is None


def test_prune_workspaces(content: Path, data_dir: Path, tmp_path: Path):
    for name in ("20260101-a", "20260102-b", "20260103-c"):
        _wf_run(content, data_dir, tmp_path, run_name=name, agent_config={"files": {"output/summary.md": "ok"}})
    removed = prune_workspaces(tmp_path / "runs", keep=1)
    assert [p.name for p in removed] == ["20260101-a", "20260102-b"]
    assert [p.name for p in (tmp_path / "runs").iterdir()] == ["20260103-c"]


def test_run_skill_extra_skills_are_snapshotted(content: Path, data_dir: Path, tmp_path: Path):
    helper = content / "skills" / "helper"
    (helper / "scripts").mkdir(parents=True)
    (helper / "SKILL.md").write_text("---\nname: helper\ndescription: helps\n---\n")
    result = run_skill("lister", "fake", {"data": data_dir}, workspace_root=tmp_path / "runs",
                       skill_paths=[content / "skills"], extra_skills=["helper"],
                       agent_config={"files": {"output/OUTPUT.md": "ok"}})
    ws = Path(result.workspace)
    assert (ws / ".claude/skills/helper/SKILL.md").is_file()
    assert (ws / ".claude/skills/lister/../helper/SKILL.md").is_file()   # ../<skill>/ resolves
    bad = run_skill("lister", "fake", {"data": data_dir}, workspace_root=tmp_path / "runs2",
                    skill_paths=[content / "skills"], extra_skills=["absent"])
    assert bad.status == "error" and "absent" in bad.error


def test_optional_outputs_are_asked_for_not_required(content: Path, data_dir: Path, tmp_path: Path):
    kw = dict(workspace_root=tmp_path / "runs", skill_paths=[content / "skills"],
              outputs=["output/data.json"], optional_outputs=["output/OUTPUT.md"])
    without = run_skill("lister", "fake", {"data": data_dir}, run_name="a",
                        agent_config={"files": {"output/data.json": "{}"}}, **kw)
    assert without.status == "completed" and without.outputs == ["output/data.json"]
    task = (Path(without.workspace) / "task.md").read_text()
    assert "## Also write, if you can\n\n- `output/OUTPUT.md`" in task
    both = run_skill("lister", "fake", {"data": data_dir}, run_name="b",
                     agent_config={"files": {"output/data.json": "{}", "output/OUTPUT.md": "hi"}}, **kw)
    assert both.outputs == ["output/data.json", "output/OUTPUT.md"]
    assert both.deliverables == ["output/data.json", "output/OUTPUT.md"]


def test_on_success_runs_for_a_partial_run_whose_outputs_pass(content: Path, data_dir: Path, tmp_path: Path):
    seen = []
    ok = _wf_run(content, data_dir, tmp_path, run_name="ok", on_success=lambda r: seen.append(r.status) or "saved",
                 agent_config={"fail": True, "files": {"output/summary.md": "ok"}})
    assert ok.status == "partial" and ok.handled == "saved" and seen == ["partial"]
    bad = _wf_run(content, data_dir, tmp_path, run_name="bad", on_success=lambda r: seen.append("bad"),
                  agent_config={"fail": True, "files": {"output/summary.md": "nope"}})
    assert bad.status == "partial" and bad.handled is None and seen == ["partial"]
    assert bad.error.startswith("timed out; ")          # the check failure is reported too


def test_on_success_exception_makes_the_run_an_error(content: Path, data_dir: Path, tmp_path: Path):
    def boom(_):
        raise RuntimeError("db down")
    result = _wf_run(content, data_dir, tmp_path, on_success=boom, agent_config={"files": {"output/summary.md": "ok"}})
    assert result.status == "error" and result.error == "db down"
    assert json.loads((Path(result.workspace) / "result.json").read_text())["status"] == "error"


def test_pricing_current_models():
    assert estimate_cost("claude-opus-5-5", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}) == 24.0
    assert estimate_cost("claude-sonnet-5-5", {"cache_read_input_tokens": 1_000_000}) == 0.2


def test_pricing_edge_cases():
    assert estimate_cost("claude-opus-4-8", {"input_tokens": 0, "output_tokens": 0}) is None   # unknown, not $0
    assert estimate_cost("claude-opus-4-8", {"cache_read_tokens": 1_000_000, "cache_write_tokens": 1_000_000}) == 6.75
    assert estimate_cost("claude-opus-4-8-20260101", {"input_tokens": 1_000_000}) == 5.0
    assert estimate_cost(None, {"input_tokens": 100}) is None


def test_cancel_stops_the_agent_and_skips_on_success(content: Path, data_dir: Path, tmp_path: Path):
    import threading
    import time as _time
    from agent_harness import AgentEvent, Capabilities, register_driver

    class Slow:
        name = "slow"
        capabilities = Capabilities(tools=True, streaming=True, resume=False, cloud=False)

        def __init__(self, config):
            self.stop = threading.Event()

        def run(self, workspace, task, policy):
            (workspace / "output" / "summary.md").write_text("ok")
            yield AgentEvent("status", self.name, {"phase": "init"})
            self.stop.wait(10)                      # until cancel() is called
            yield AgentEvent("error", self.name, {"message": "terminated", "fatal": True})

        def cancel(self):
            self.stop.set()

    register_driver("slow", Slow)
    cancel, saved = threading.Event(), []
    threading.Timer(0.2, cancel.set).start()
    started = _time.time()
    result = run_workflow("summarize", "slow", {"data_dir": str(data_dir)}, workspace_root=tmp_path / "runs",
                          workflow_paths=[content / "workflows"], skill_paths=[content / "skills"],
                          cancel=cancel, on_success=saved.append)
    assert result.status == "cancelled" and saved == [] and _time.time() - started < 5
    assert json.loads((Path(result.workspace) / "state.json").read_text())["status"] == "cancelled"

    early = run_workflow("summarize", "slow", {"data_dir": str(data_dir)}, workspace_root=tmp_path / "runs2",
                         workflow_paths=[content / "workflows"], skill_paths=[content / "skills"], cancel=cancel)
    assert early.status == "cancelled" and early.outputs == []      # set before start: the agent never runs


def test_prune_workspaces_by_pattern(tmp_path: Path):
    root = tmp_path / "runs"
    names = ["20260101-000000-a-x", "20260102-000000-a-x", "20260103-000000-a-x",
             "20260101-000000-b-x", "20260102-000000-b-x"]
    for name in names:
        (root / name).mkdir(parents=True)
        (root / name / "state.json").write_text("{}")
    removed = prune_workspaces(root, keep=1, match="????????-??????-a-x")
    assert sorted(p.name for p in removed) == ["20260101-000000-a-x", "20260102-000000-a-x"]
    assert sorted(p.name for p in root.iterdir()) == ["20260101-000000-b-x", "20260102-000000-b-x",
                                                      "20260103-000000-a-x"]     # b untouched


def test_validate_reports_every_name_problem(tmp_path: Path):
    d = tmp_path / "good-name"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: Bad_Name\ndescription: x\n---\n")
    errors = validate_skill_dir(d)
    assert any("kebab-case" in e for e in errors) and any("folder name" in e for e in errors)
