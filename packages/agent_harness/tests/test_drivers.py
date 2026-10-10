from __future__ import annotations

from pathlib import Path

from agent_harness.drivers.claude_code import ClaudeCodeDriver, _redact
from agent_harness.drivers.prompt_inject import PromptInjectDriver, is_local_url, parse_files
from agent_harness.policy import Policy
from agent_harness.runtime import AgentTask


def _task(**kw) -> AgentTask:
    base = dict(prompt="do it", outputs=["output/a.md"], system_context="rules", python="/venv/bin/python",
                read_dirs=[Path("/data/run1")])
    base.update(kw)
    return AgentTask(**base)


def test_claude_command_is_locked_down():
    cmd = ClaudeCodeDriver({"model": "sonnet"}).build_command(_task(), Policy(max_turns=7))
    assert cmd[:3] == ["claude", "-p", "do it"]
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    assert "--strict-mcp-config" in cmd
    assert "--dangerously-skip-permissions" not in cmd
    assert "Bash(/venv/bin/python .claude/skills/*)" in cmd
    assert "Write(./output/**)" in cmd
    assert "Bash(/venv/bin/python workflow/checks/*)" in cmd
    assert cmd[cmd.index("--add-dir") + 1] == "/data/run1"
    assert cmd[cmd.index("--max-turns") + 1] == "7"
    assert cmd[cmd.index("--append-system-prompt") + 1] == "rules"
    assert "WebFetch" not in " ".join(cmd) and "WebSearch" not in " ".join(cmd)


def test_redact_hides_prompt():
    assert _redact(["claude", "-p", "secret prompt", "--model", "x"]) == ["claude", "-p", "<13 chars>", "--model", "x"]


def test_claude_translate_stream():
    d = ClaudeCodeDriver({})
    init = list(d._translate({"type": "system", "subtype": "init", "model": "m", "session_id": "s"}))
    assert init[0].type == "status" and init[0].data["model"] == "m"
    msg = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "id": "1", "name": "Bash", "input": {"command": "ls"}},
        {"type": "tool_use", "id": "2", "name": "Write", "input": {"file_path": "output/a.md"}},
        {"type": "tool_use", "id": "3", "name": "Read", "input": {"file_path": "x"}}]}}
    assert [e.type for e in d._translate(msg)] == ["message", "command", "file_change", "tool_call"]
    res = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "1",
                                                    "content": [{"type": "text", "text": "out"}]}]}}
    assert list(d._translate(res))[0].data["content"] == "out"
    final = list(d._translate({"type": "result", "result": "done", "total_cost_usd": 0.1, "num_turns": 3,
                               "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "curl"}}]}))
    assert [e.type for e in final] == ["usage", "permission_request", "completed"]
    assert final[-1].data["message"] == "done"


def test_prompt_inject_residency():
    assert PromptInjectDriver({"base_url": "http://localhost:11434/v1"}).capabilities.cloud is False
    assert PromptInjectDriver({"base_url": "http://10.0.0.5/v1"}).capabilities.cloud is False
    assert PromptInjectDriver({"base_url": "https://api.openai.com/v1"}).capabilities.cloud is True
    assert is_local_url("http://127.0.0.1:8000") and not is_local_url("")


def test_parse_files_only_accepts_declared_outputs():
    outputs = ["output/a.md", "output/b.json"]
    raw = '```json\n{"output/a.md": "text", "output/b.json": {"k": 1}, "../evil": "x", "output/c": "y"}\n```'
    assert parse_files(raw, outputs) == {"output/a.md": "text", "output/b.json": {"k": 1}}
    assert parse_files("not json", outputs) == {}


def _anthropic_stub(monkeypatch, *, stop_reason="end_turn", text='{"output/a.md": "hi"}'):
    import sys
    import types
    calls = {}

    class _Messages:
        def create(self, **kw):
            calls["create"] = kw
            block = types.SimpleNamespace(type="text", text=text)
            usage = types.SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=None,
                                          cache_read_input_tokens=0)
            return types.SimpleNamespace(content=[block], usage=usage, stop_reason=stop_reason)

    class _Client:
        def __init__(self, **kw):
            calls["client"] = kw
            self.messages = _Messages()

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=_Client))
    return calls


def test_prompt_inject_anthropic_provider(tmp_path: Path, monkeypatch):
    calls = _anthropic_stub(monkeypatch)
    (tmp_path / "output").mkdir()
    driver = PromptInjectDriver({"provider": "anthropic", "api_key": "k", "model": "claude-opus-5-5"})
    assert driver.capabilities.cloud is True
    events = list(driver.run(tmp_path, _task(), Policy()))
    assert [e.type for e in events] == ["status", "usage", "file_change", "completed"]
    assert events[1].data == {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0}
    assert (tmp_path / "output" / "a.md").read_text() == "hi"
    assert calls["client"]["api_key"] == "k" and "base_url" not in calls["client"]
    assert calls["create"]["model"] == "claude-opus-5-5" and calls["create"]["system"].startswith("rules")


def test_prompt_inject_anthropic_refusal_is_fatal(tmp_path: Path, monkeypatch):
    _anthropic_stub(monkeypatch, stop_reason="refusal", text="")
    (tmp_path / "output").mkdir()
    events = list(PromptInjectDriver({"provider": "anthropic"}).run(tmp_path, _task(), Policy()))
    assert events[-1].type == "error" and events[-1].data["fatal"] and "refusal" in events[-1].data["message"]


def test_prompt_inject_rejects_unknown_provider():
    import pytest
    with pytest.raises(ValueError):
        PromptInjectDriver({"provider": "nope"})


def test_prompt_inject_inlines_inputs_without_context_commands(tmp_path: Path):
    data = tmp_path / "data"
    (data / "docs").mkdir(parents=True)
    (data / "deal.json").write_text('{"city": "Boise"}')
    (data / "docs" / "scan.pdf").write_bytes(b"%PDF")
    ws = tmp_path / "ws"
    (ws / "inputs").mkdir(parents=True)
    (ws / "inputs" / "deal").symlink_to(data, target_is_directory=True)
    user = PromptInjectDriver({"base_url": "http://localhost:1"}).build_messages(ws, _task())[1]["content"]
    assert '## inputs/deal/deal.json\n\n{"city": "Boise"}' in user
    assert "## inputs/deal/docs/scan.pdf\n\n(binary file, not included)" in user
    with_cmds = PromptInjectDriver({"base_url": "http://localhost:1"}).build_messages(
        ws, _task(context_commands=[{"title": "t", "command": "echo hi"}]))[1]["content"]
    assert "Boise" not in with_cmds and "## t\n\nhi" in with_cmds


def test_claude_allows_exact_self_check_commands():
    cmd = ClaudeCodeDriver({}).build_command(_task(self_checks=["py -m app.checks leads output/leads.json"]), Policy())
    assert "Bash(py -m app.checks leads output/leads.json)" in cmd
