from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agent_harness.runtime import register_driver
from tests.fixtures.fake_agent import FakeAgent

register_driver("fake", FakeAgent)


@pytest.fixture
def content(tmp_path: Path) -> Path:
    """A minimal workflow + skill tree, independent of any application."""
    root = tmp_path / "content"
    wf = root / "workflows" / "summarize"
    wf.mkdir(parents=True)
    (wf / "WORKFLOW.md").write_text(textwrap.dedent("""\
        ---
        name: summarize
        description: Summarize a directory.
        inputs:
          data_dir: {type: path, required: true}
          audience: {type: string, required: false}
        skills: [lister]
        outputs: [output/summary.md]
        checks:
          - "{python} -c \\"import pathlib,sys; sys.exit(0 if 'ok' in pathlib.Path('output/summary.md').read_text() else 1)\\""
        policy: {max_minutes: 5}
        stages: [01-gather, 02-write]
        context_commands:
          - title: listing
            command: "{python} .claude/skills/lister/scripts/ls.py {input.data_dir}"
        ---
        Summarize {input.data_dir} for the reader. Literal {braces} survive.
        """))
    skill = root / "skills" / "lister"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: lister\ndescription: Lists files.\n---\nUse scripts/ls.py.\n")
    (skill / "scripts" / "ls.py").write_text("import os,sys; print(sorted(os.listdir(sys.argv[1])))\n")
    return root


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    (d / "a.txt").write_text("hello")
    return d
