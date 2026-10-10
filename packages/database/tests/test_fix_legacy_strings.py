"""coverage for database.fix_legacy_strings (was 0%, and unimportable).

The module was dead: it imported its siblings flat, so `import
database.fix_legacy_strings` raised ModuleNotFoundError and running it by path
died in sqlite_database's relative import. Nothing could execute it. Now that
the imports are relative, these tests drive infer_type's type-guessing ladder
and fix_legacy_table against a real SQLite file.
"""
import json
import runpy
import sqlite3
import sys
from unittest.mock import patch

import pytest

from database.fix_legacy_strings import fix_legacy_table, infer_type


class TestInferType:
    @pytest.mark.parametrize("value", [None, 1, 1.5, True, ["a"], {"k": "v"}])
    def test_non_strings_pass_straight_through(self, value):
        assert infer_type(value) == value

    @pytest.mark.parametrize("value", ["", "   ", "\t\n"])
    def test_blank_strings_collapse_to_empty(self, value):
        assert infer_type(value) == ""

    @pytest.mark.parametrize("value", ["true", "TRUE", "True", " true "])
    def test_truthy_spellings_become_true(self, value):
        assert infer_type(value) is True

    @pytest.mark.parametrize("value", ["false", "FALSE", "False"])
    def test_falsy_spellings_become_false(self, value):
        assert infer_type(value) is False

    @pytest.mark.parametrize("value", ["none", "NULL", "None", "null"])
    def test_null_spellings_become_none(self, value):
        assert infer_type(value) is None

    @pytest.mark.parametrize("value,expected", [("0", 0), ("42", 42), ("-42", -42)])
    def test_integers_are_parsed(self, value, expected):
        assert infer_type(value) == expected

    @pytest.mark.parametrize("value,expected", [("1.5", 1.5), ("-2.25", -2.25), ("1e3", 1000.0)])
    def test_floats_are_parsed(self, value, expected):
        assert infer_type(value) == expected

    def test_a_bare_minus_stays_a_string(self):
        # "-"[1:] is "", which is not isdigit(), so the int branch declines it.
        assert infer_type("-") == "-"

    def test_a_digit_like_char_that_int_rejects_falls_through(self):
        # "²".isdigit() is True but int("²") raises -- this is the `except
        # Exception: pass` arm under the int attempt, and it must not escape.
        assert infer_type("²") == "²"

    def test_a_dotted_non_number_falls_through_to_the_string(self):
        # Reaches float("a.b"), which raises, then json.loads, which also raises.
        assert infer_type("a.b") == "a.b"

    def test_json_objects_and_arrays_are_parsed(self):
        assert infer_type('{"a": 1}') == {"a": 1}
        assert infer_type("[1, 2]") == [1, 2]

    def test_plain_text_is_returned_unchanged(self):
        assert infer_type("hello world") == "hello world"


@pytest.fixture
def legacy_db(tmp_path):
    """A table whose values are all strings, as the legacy writer left them."""
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT, age TEXT, ok TEXT)")
    conn.execute("INSERT INTO widgets VALUES (1, 'bolt', '30', 'true')")
    conn.execute("INSERT INTO widgets VALUES (2, 'nut', '7', 'false')")
    conn.commit()
    conn.close()
    return path


def _row(path, table="widgets", key=1, id_col="id"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute(f"SELECT * FROM {table} WHERE {id_col}=?", (key,)).fetchone())
    conn.close()
    return row


class TestFixLegacyTable:
    def test_string_values_are_rewritten_with_their_inferred_types(self, legacy_db, capsys):
        fix_legacy_table(legacy_db, "widgets")
        row = _row(legacy_db)
        assert json.loads(row["age"]) == {"__type__": "int", "value": 30}
        assert json.loads(row["ok"]) == {"__type__": "bool", "value": True}
        assert "has been fixed with type info" in capsys.readouterr().out

    def test_plain_strings_are_left_as_plain_strings(self, legacy_db):
        fix_legacy_table(legacy_db, "widgets")
        assert _row(legacy_db)["name"] == "bolt"

    def test_the_id_column_is_never_rewritten(self, legacy_db):
        fix_legacy_table(legacy_db, "widgets")
        assert _row(legacy_db)["id"] == 1

    def test_every_row_is_processed(self, legacy_db):
        fix_legacy_table(legacy_db, "widgets")
        assert json.loads(_row(legacy_db, key=2)["age"]) == {"__type__": "int", "value": 7}

    def test_a_custom_id_column_is_honoured(self, tmp_path):
        path = str(tmp_path / "custom.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t (pk INTEGER PRIMARY KEY, age TEXT)")
        conn.execute("INSERT INTO t VALUES (1, '5')")
        conn.commit()
        conn.close()
        fix_legacy_table(path, "t", id_col="pk")
        assert json.loads(_row(path, table="t", id_col="pk")["age"]) == {
            "__type__": "int",
            "value": 5,
        }

    def test_a_table_with_only_an_id_column_is_a_no_op(self, tmp_path):
        # The `if set_clause:` guard -- with nothing but the id there is no SET
        # clause to build, and the UPDATE must be skipped rather than emitted
        # as invalid SQL.
        path = str(tmp_path / "bare.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()
        fix_legacy_table(path, "t")
        assert _row(path, table="t") == {"id": 1}

    def test_an_empty_table_is_a_no_op(self, tmp_path):
        path = str(tmp_path / "empty.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, age TEXT)")
        conn.commit()
        conn.close()
        fix_legacy_table(path, "t")  # must not raise

    @pytest.mark.parametrize("bad", ["widgets; DROP TABLE widgets", "wid gets", "1widgets", ""])
    def test_an_unsafe_table_name_is_rejected_before_any_sql_runs(self, legacy_db, bad):
        with pytest.raises(ValueError):
            fix_legacy_table(legacy_db, bad)

    def test_an_unsafe_id_column_is_rejected(self, legacy_db):
        with pytest.raises(ValueError):
            fix_legacy_table(legacy_db, "widgets", id_col="id; DROP TABLE widgets")


# run_module warns because the module is already imported by the tests above.
# That is exactly what we want -- re-executing the imported module under
# __main__ is the point -- so the warning is noise here, not a signal.
@pytest.mark.filterwarnings("ignore:.*found in sys.modules.*:RuntimeWarning")
class TestCommandLine:
    """The __main__ block, driven through runpy so it executes in-process."""

    def _run(self, argv):
        with patch.object(sys, "argv", argv):
            runpy.run_module(
                "database.fix_legacy_strings", run_name="__main__", alter_sys=True
            )

    def test_too_few_arguments_exits_one_with_usage(self, capsys):
        with pytest.raises(SystemExit) as exc:
            self._run(["fix_legacy_strings", "only-a-path"])
        assert exc.value.code == 1
        assert "Usage: python -m database.fix_legacy_strings" in capsys.readouterr().out

    def test_it_fixes_the_named_table(self, legacy_db):
        self._run(["fix_legacy_strings", legacy_db, "widgets"])
        assert json.loads(_row(legacy_db)["age"]) == {"__type__": "int", "value": 30}

    def test_a_third_argument_overrides_the_id_column(self, tmp_path):
        path = str(tmp_path / "custom.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t (pk INTEGER PRIMARY KEY, age TEXT)")
        conn.execute("INSERT INTO t VALUES (1, '5')")
        conn.commit()
        conn.close()
        self._run(["fix_legacy_strings", path, "t", "pk"])
        assert json.loads(_row(path, table="t", id_col="pk")["age"]) == {
            "__type__": "int",
            "value": 5,
        }
