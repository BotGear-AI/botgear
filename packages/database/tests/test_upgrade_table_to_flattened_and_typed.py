"""HYR-945: regression guard for database.upgrade_table_to_flattened_and_typed.

This module is almost entirely an `if __name__ == "__main__"` block, which
packages.coveragerc excludes, so these tests move the coverage number barely at
all. They are here for correctness, not for the percentage: the script
subprocessed its two steps by file path, and running either of those files by
path has never worked -- convert_to_flattened.py dies in its own relative
import before doing anything. Step 1 failed on every invocation, so the whole
script was dead. The `-m` form these tests pin is what makes it run.

The end-to-end case actually spawns the two subprocesses against a real SQLite
file; the rest patch subprocess.run to assert the argv without paying for it.
"""
import json
import runpy
import sqlite3
import subprocess
import sys
from unittest.mock import patch

import pytest

MODULE = "database.upgrade_table_to_flattened_and_typed"


@pytest.mark.filterwarnings("ignore:.*found in sys.modules.*:RuntimeWarning")
class TestArgumentHandling:
    def _run(self, argv):
        with patch.object(sys, "argv", argv):
            with patch("subprocess.run") as run:
                runpy.run_module(MODULE, run_name="__main__", alter_sys=True)
        return run

    def test_too_few_arguments_exits_one_with_usage(self, capsys):
        with patch.object(sys, "argv", ["upgrade", "only-a-path"]):
            with pytest.raises(SystemExit) as exc:
                runpy.run_module(MODULE, run_name="__main__", alter_sys=True)
        assert exc.value.code == 1
        assert f"Usage: python -m {MODULE}" in capsys.readouterr().out

    def test_both_steps_run_as_modules_not_file_paths(self):
        run = self._run(["upgrade", "/tmp/db.sqlite", "widgets"])
        assert run.call_count == 2
        first, second = (c.args[0] for c in run.call_args_list)
        assert first[1:3] == ["-m", "database.convert_to_flattened"]
        assert second[1:3] == ["-m", "database.fix_legacy_strings"]
        # The old form passed a filesystem path here, which is what broke it.
        assert not any(a.endswith(".py") for a in first + second)

    def test_the_steps_use_the_running_interpreter(self):
        run = self._run(["upgrade", "/tmp/db.sqlite", "widgets"])
        assert all(c.args[0][0] == sys.executable for c in run.call_args_list)

    def test_defaults_are_json_and_id(self):
        run = self._run(["upgrade", "/tmp/db.sqlite", "widgets"])
        first, second = (c.args[0] for c in run.call_args_list)
        assert first[3:] == ["/tmp/db.sqlite", "widgets", "json", "id"]
        assert second[3:] == ["/tmp/db.sqlite", "widgets", "id"]

    def test_the_json_and_id_columns_can_be_overridden(self):
        run = self._run(["upgrade", "/tmp/db.sqlite", "widgets", "payload", "pk"])
        first, second = (c.args[0] for c in run.call_args_list)
        assert first[3:] == ["/tmp/db.sqlite", "widgets", "payload", "pk"]
        assert second[3:] == ["/tmp/db.sqlite", "widgets", "pk"]

    def test_a_failing_first_step_aborts_before_the_second(self):
        with patch.object(sys, "argv", ["upgrade", "/tmp/db.sqlite", "widgets"]):
            with patch(
                "subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "cmd"),
            ) as run:
                with pytest.raises(subprocess.CalledProcessError):
                    runpy.run_module(MODULE, run_name="__main__", alter_sys=True)
        assert run.call_count == 1

    def test_check_is_true_on_both_steps(self):
        run = self._run(["upgrade", "/tmp/db.sqlite", "widgets"])
        assert all(c.kwargs["check"] is True for c in run.call_args_list)


def test_the_two_steps_actually_run_end_to_end(tmp_path):
    """The whole point: both subprocesses succeed against a real database.

    Before the `-m` change this failed at step 1 with
    "attempted relative import with no known parent package".
    """
    path = tmp_path / "up.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, json TEXT)")
    conn.execute(
        "INSERT INTO widgets VALUES (1, ?)",
        (json.dumps({"name": "bolt", "age": "30", "ok": "true"}),),
    )
    conn.commit()
    conn.close()

    result = subprocess.run(
        [sys.executable, "-m", MODULE, str(path), "widgets"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM widgets WHERE id=1").fetchone())
    conn.close()

    # Step 1 added a column per JSON key; step 2 typed the values.
    assert row["name"] == "bolt"
    assert json.loads(row["age"]) == {"__type__": "int", "value": 30}
    assert json.loads(row["ok"]) == {"__type__": "bool", "value": True}
