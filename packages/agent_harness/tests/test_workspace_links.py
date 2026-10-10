"""inputs/<name> links: relative (so they resolve on whichever host owns a
network-mounted tree), and tolerant of filesystems that create the link but
report an error anyway (sshfs with follow_symlinks)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_harness.workspace import build_workspace


def _build(tmp_path: Path, target: Path):
    return build_workspace(tmp_path / "runs", "run1", inputs={"run_dir": str(target)},
                           path_inputs={"run_dir"}, skills={}, agents_md="")


def test_input_links_are_relative_and_resolve(tmp_path: Path):
    target = tmp_path / "data" / "work" / "r1"
    target.mkdir(parents=True)
    (target / "report.json").write_text("{}")
    link = _build(tmp_path, target).inputs / "run_dir"
    assert not os.path.isabs(os.readlink(link))
    assert (link / "report.json").read_text() == "{}"


def test_link_inside_its_own_target_dir(tmp_path: Path):
    """The report interpreter links a run dir into a workspace under that same run dir."""
    run_dir = tmp_path / "work" / "r1"
    run_dir.mkdir(parents=True)
    ws = build_workspace(run_dir / "agent_runs", "run1", inputs={"run_dir": str(run_dir)},
                         path_inputs={"run_dir"}, skills={}, agents_md="")
    assert os.readlink(ws.inputs / "run_dir") == os.path.join("..", "..", "..")
    assert (ws.inputs / "run_dir" / "agent_runs").is_dir()


def test_an_error_after_the_link_was_created_is_ignored(tmp_path: Path, monkeypatch):
    target = tmp_path / "data"
    target.mkdir()
    real = Path.symlink_to

    def create_then_fail(self, *args, **kwargs):
        real(self, *args, **kwargs)
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(Path, "symlink_to", create_then_fail)

    assert (_build(tmp_path, target).inputs / "run_dir").exists()


def test_a_real_link_failure_still_raises(tmp_path: Path, monkeypatch):
    target = tmp_path / "data"
    target.mkdir()

    def fail(self, *args, **kwargs):
        raise OSError(2, "No such file or directory")
    monkeypatch.setattr(Path, "symlink_to", fail)

    with pytest.raises(OSError):
        _build(tmp_path, target)
