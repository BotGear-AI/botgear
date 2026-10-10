"""Durable workflow state (state.json).

Workflow state lives in files, never in an agent's conversation: any agent
can pick up a run from state.json plus the artifacts in output/. Stage
progress is derived from which output/<stage>/ folders exist, so agents
never have to write state themselves.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

STATE_FILE = "state.json"


@dataclass
class RunState:
    workflow: str
    agent: str
    status: str = "created"          # created | running | completed | failed_checks | error | cancelled
    stages: list[str] = field(default_factory=list)
    completed_stages: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    session_id: Optional[str] = None
    started: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    error: Optional[str] = None

    def refresh_from_workspace(self, root: Path) -> None:
        output = root / "output"
        self.completed_stages = [s for s in self.stages if (output / s).is_dir() and any((output / s).iterdir())]
        self.artifacts = sorted(
            str(p.relative_to(root)) for p in output.rglob("*") if p.is_file()
        ) if output.is_dir() else []

    def save(self, root: Path) -> None:
        self.updated = time.time()
        (root / STATE_FILE).write_text(json.dumps(asdict(self), indent=2, default=str), encoding="utf-8")


def load_state(root: Path) -> RunState:
    return RunState(**json.loads((root / STATE_FILE).read_text(encoding="utf-8")))
