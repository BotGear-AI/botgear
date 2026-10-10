"""MCP servers in workflows: parsing, the policy gate, task.md, the Claude
Code command, and checks that see secrets (redacted)."""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from agent_harness import run_workflow
from agent_harness.drivers.claude_code import ClaudeCodeDriver, _mcp_ca_bundle
from agent_harness.mcp_servers import McpServer, McpSpecError, env_ref_headers, parse_mcp
from agent_harness.policy import Policy
from agent_harness.runtime import AgentTask, register_driver
from agent_harness.workflow import WorkflowError, load_workflow, render_task
from tests.fixtures.fake_agent import FakeAgent

SPEC = {"robot": {"url": "{input.endpoint}", "headers": {"X-API-Key": "{secret.ROBOT_KEY}"},
                  "tools": ["list_capabilities", "get_state"], "description": "the live robot"}}


def _wf(root: Path, *, mcp: str, policy: str = "{mcp: allow}", secrets: str = "[ROBOT_KEY]",
        checks: str = "[]") -> Path:
    d = root / "workflows" / "inspect"
    d.mkdir(parents=True)
    lines = [
        "---",
        "name: inspect",
        "description: Inspect a live system.",
        "inputs:",
        "  endpoint: {type: string, required: true}",
        "outputs: [output/report.md]",
        f"secrets: {secrets}",
        f"policy: {policy}",
        f"checks: {checks}",
        "mcp:",
        textwrap.indent(mcp, "  "),
        "---",
        "Report what is live.",
    ]
    (d / "WORKFLOW.md").write_text("\n".join(lines) + "\n")
    return root / "workflows"


MCP_YAML = """robot:
  url: "{input.endpoint}"
  headers: {X-API-Key: "{secret.ROBOT_KEY}"}
  tools: [list_capabilities, get_state]
  description: the live robot"""


# ─── parsing ────────────────────────────────────────────────────────────


def test_parse_mcp_ok_and_secret_refs():
    servers = parse_mcp(SPEC, ["ROBOT_KEY"], "w")
    assert servers["robot"].secret_refs() == {"ROBOT_KEY"}
    assert env_ref_headers(servers["robot"]) == {"X-API-Key": "${ROBOT_KEY}"}


@pytest.mark.parametrize("bad, msg", [
    ({"bad name!": {"url": "u", "tools": ["*"]}}, "must match"),
    ({"robot": {"tools": ["*"]}}, "needs a url"),
    ({"robot": {"url": "u"}}, "needs `tools`"),
    ({"robot": {"url": "u", "tools": []}}, "needs `tools`"),
    ({"robot": {"url": "u", "tools": ["*"], "headers": {"K": "{secret.OTHER}"}}}, "undeclared secrets"),
    ("not-a-map", "must be a mapping"),
])
def test_parse_mcp_rejects(bad, msg):
    with pytest.raises(McpSpecError, match=msg):
        parse_mcp(bad, ["ROBOT_KEY"], "w")


def test_workflow_loads_mcp_and_rejects_bad_spec(tmp_path):
    wf = load_workflow("inspect", [_wf(tmp_path / "a", mcp=MCP_YAML)])
    assert wf.mcp["robot"].tools == ["list_capabilities", "get_state"]
    with pytest.raises(WorkflowError, match="undeclared secrets"):
        load_workflow("inspect", [_wf(tmp_path / "b", mcp=MCP_YAML, secrets="[]")])


def test_task_md_lists_live_tools_for_tool_agents(tmp_path):
    wf = load_workflow("inspect", [_wf(tmp_path, mcp=MCP_YAML)])
    md = render_task(wf, {"endpoint": "https://x/mcp"}, {}, {"python": "py"}, tools=True)
    assert "## Live tools (MCP)" in md and "`robot` - the live robot" in md
    assert "`list_capabilities`, `get_state`" in md and "{secret." not in md
    assert "## Live tools" not in render_task(wf, {"endpoint": "x"}, {}, {}, tools=False)


# ─── policy gate ────────────────────────────────────────────────────────


def test_policy_mcp_defaults_to_deny():
    assert Policy().mcp == "deny"
    assert Policy.from_dict({"mcp": "allow"}).mcp == "allow"


def _run(tmp_path, wf_paths, agent="fake", policy=None, config=None, **kw):
    return run_workflow("inspect", agent, {"endpoint": "https://robot.local/v1/mcp"},
                        workspace_root=tmp_path / "runs", workflow_paths=[wf_paths], skill_paths=[],
                        policy=policy, agent_config=config or {"mcp": True, "files": {"output/report.md": "ok"}},
                        secrets={"ROBOT_KEY": "k-12345"}, **kw)


def test_mcp_needs_policy_allow(tmp_path):
    res = _run(tmp_path, _wf(tmp_path, mcp=MCP_YAML, policy="{}"))
    assert res.status == "error" and "policy mcp=deny" in res.error
    assert not (tmp_path / "runs").exists() or not any((tmp_path / "runs").iterdir())   # nothing staged


def test_caller_can_deny_mcp(tmp_path):
    res = _run(tmp_path, _wf(tmp_path, mcp=MCP_YAML), policy={"mcp": "deny"})
    assert res.status == "error" and "mcp=deny" in res.error


def test_agent_without_mcp_is_refused(tmp_path):
    res = _run(tmp_path, _wf(tmp_path, mcp=MCP_YAML), config={"files": {"output/report.md": "ok"}})
    assert res.status == "error" and "can't use MCP servers" in res.error


def test_task_carries_resolved_servers(tmp_path):
    seen = {}

    def factory(config):
        agent = FakeAgent(config)
        seen["agent"] = agent
        return agent
    register_driver("fake-capture", factory)
    res = _run(tmp_path, _wf(tmp_path, mcp=MCP_YAML), agent="fake-capture")
    assert res.status == "completed", res.error
    server = seen["agent"].seen_task.mcp_servers[0]
    assert server.url == "https://robot.local/v1/mcp" and server.headers == {"X-API-Key": "{secret.ROBOT_KEY}"}


# ─── checks see secrets, output redacted ────────────────────────────────


def test_checks_get_secrets_and_are_redacted(tmp_path):
    check = f'"{sys.executable} -c \\"import os; print(\'key=\' + os.environ[\'ROBOT_KEY\']); raise SystemExit(1)\\""'
    res = _run(tmp_path, _wf(tmp_path, mcp=MCP_YAML, checks=f"[{check}]"))
    assert res.status == "failed_checks"
    checks = json.loads((Path(res.workspace) / "checks.json").read_text())
    assert "key=***" in checks[-1]["detail"] and "k-12345" not in json.dumps(checks)


# ─── Claude Code command ────────────────────────────────────────────────


def _task(**kw) -> AgentTask:
    base = dict(prompt="p", outputs=["output/a.md"], python="py")
    base.update(kw)
    return AgentTask(**base)


ROBOT = McpServer(name="robot", url="https://robot.local/v1/mcp", headers={"X-API-Key": "{secret.ROBOT_KEY}"},
                  tools=["list_capabilities", "switch-1__power_set"])


def test_claude_command_with_mcp():
    cmd = ClaudeCodeDriver({}).build_command(_task(mcp_servers=[ROBOT]), Policy(mcp="allow"))
    assert "--strict-mcp-config" in cmd
    config = json.loads(cmd[cmd.index("--mcp-config") + 1])
    assert config == {"mcpServers": {"robot": {"type": "http", "url": "https://robot.local/v1/mcp",
                                               "headers": {"X-API-Key": "${ROBOT_KEY}"}}}}
    assert "mcp__robot__list_capabilities" in cmd and "mcp__robot__switch-1__power_set" in cmd
    assert "mcp__robot" not in cmd                                      # not the whole server


def test_claude_command_all_tools_and_policy_deny():
    star = McpServer(name="robot", url="u", tools=["*"])
    cmd = ClaudeCodeDriver({}).build_command(_task(mcp_servers=[star]), Policy(mcp="allow"))
    assert "mcp__robot" in cmd and "headers" not in cmd[cmd.index("--mcp-config") + 1]
    denied = ClaudeCodeDriver({}).build_command(_task(mcp_servers=[star]), Policy())
    assert "--mcp-config" not in denied and "mcp__robot" not in denied
    assert "--mcp-config" not in ClaudeCodeDriver({}).build_command(_task(), Policy(mcp="allow"))


def test_ca_bundle(tmp_path):
    a, b = tmp_path / "a.pem", tmp_path / "b.pem"
    a.write_text("A")
    b.write_text("B")
    assert _mcp_ca_bundle(tmp_path, _task()) is None
    one = McpServer(name="r", url="u", tools=["*"], ca_cert=str(a))
    assert _mcp_ca_bundle(tmp_path, _task(mcp_servers=[one])) == str(a)
    two = McpServer(name="s", url="u", tools=["*"], ca_cert=str(b))
    bundle = _mcp_ca_bundle(tmp_path, _task(mcp_servers=[one, two]))
    assert Path(bundle).read_text() == "A\nB\n"


def test_relative_ca_cert_resolves_against_workspace(tmp_path):
    from agent_harness.workflow import resolve_mcp
    wf = load_workflow("inspect", [_wf(tmp_path, mcp=MCP_YAML + '\n  ca_cert: "{input.ca}"')])
    real = tmp_path / "real.pem"
    real.write_text("PEM")
    ws = tmp_path / "ws"
    (ws / "inputs").mkdir(parents=True)
    (ws / "inputs" / "ca").symlink_to(real)
    server = resolve_mcp(wf, {"workspace": ws, "input.ca": "inputs/ca", "input.endpoint": "u"})[0]
    assert server.ca_cert == str(real.resolve())
