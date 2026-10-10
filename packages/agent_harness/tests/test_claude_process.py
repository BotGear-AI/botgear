"""The Claude Code driver's process handling, against scripted fake CLIs
(no vendor): a noisy stderr must not deadlock the stream, and a timeout must
end the run even if the process ignores SIGTERM."""
from __future__ import annotations

import sys
import textwrap
import time
from pathlib import Path

from agent_harness.drivers.claude_code import ClaudeCodeDriver
from agent_harness.policy import Policy
from agent_harness.runtime import AgentTask

RESULT = '{"type": "result", "result": "done", "total_cost_usd": 0.01}'


def _cli(tmp_path: Path, body: str) -> str:
    path = tmp_path / "fake_claude"
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return str(path)


def _run(cli: str, tmp_path: Path, policy: Policy) -> tuple[list, float]:
    driver = ClaudeCodeDriver({"cli": cli})
    driver.kill_grace_s = 0.5
    started = time.time()
    events = list(driver.run(tmp_path, AgentTask(prompt="p", outputs=[]), policy))
    return events, time.time() - started


def test_large_stderr_does_not_deadlock(tmp_path: Path):
    cli = _cli(tmp_path, f"""
        import sys
        sys.stderr.write("x" * 1_000_000)          # far beyond a pipe buffer
        sys.stderr.flush()
        print({RESULT!r}, flush=True)
    """)
    events, took = _run(cli, tmp_path, Policy(max_minutes=0.5))
    assert [e.type for e in events][-1] == "completed" and took < 10


def test_timeout_kills_a_process_that_ignores_sigterm(tmp_path: Path):
    cli = _cli(tmp_path, """
        import signal, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print('{"type": "system", "subtype": "init", "model": "m"}', flush=True)
        time.sleep(600)
    """)
    events, took = _run(cli, tmp_path, Policy(max_minutes=1 / 60))     # 1 s budget
    assert events[-1].type == "error" and "timed out" in events[-1].data["message"]
    assert took < 10
