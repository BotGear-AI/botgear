"""External success checks, run by the harness AFTER the agent finishes.

The agent's own opinion of its work is never the verdict. Two kinds:
  - built-in: every declared output exists and is non-empty
  - workflow `checks:` commands, run from the workspace; exit code 0 = pass
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from agent_harness.workflow import substitute

_TAIL = 2000


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


def check_outputs_exist(root: Path, outputs: list[str]) -> list[CheckResult]:
    results = []
    for rel in outputs:
        path = root / rel
        ok = path.is_file() and path.stat().st_size > 0
        results.append(CheckResult(name=f"output exists: {rel}", passed=ok,
                                   detail="" if ok else f"FAIL: {rel} is missing or empty"))
    return results


def run_check_command(root: Path, command: str, placeholders: dict[str, Any], timeout_s: int = 300,
                      env: Optional[dict[str, str]] = None) -> CheckResult:
    """``env`` (the run's declared secrets) is added to the check's
    environment, so a check can verify a live system the agent acted on."""
    rendered = substitute(command, placeholders)
    try:
        proc = subprocess.run(shlex.split(rendered), cwd=root, capture_output=True, text=True, timeout=timeout_s,
                              env={**os.environ, **env} if env else None)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(name=rendered, passed=False, detail=str(exc))
    detail = (proc.stdout[-_TAIL:] + ("\n" + proc.stderr[-_TAIL:] if proc.stderr else "")).strip()
    return CheckResult(name=rendered, passed=proc.returncode == 0, detail=detail)


def run_checks(root: Path, outputs: list[str], commands: list[str], placeholders: dict[str, Any],
               env: Optional[dict[str, str]] = None,
               redact: Optional[Callable[[str], str]] = None) -> list[CheckResult]:
    results = check_outputs_exist(root, outputs)
    results += [run_check_command(root, cmd, placeholders, env=env) for cmd in commands]
    if redact is not None:
        for r in results:
            r.detail = redact(r.detail)
    (root / "checks.json").write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
    return results
