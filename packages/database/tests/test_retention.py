"""database.retention + DatabaseAdapter.prune_older_than.

Postgres tests run against a real server when RETENTION_TEST_PG_PORT is set
(throwaway db `retention_test`, user dbuser123, password from
RETENTION_TEST_PG_PASSWORD, default "postgres"); skipped otherwise.
"""
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import database.retention as retention_module
from database.retention import (
    RetentionPolicy, as_utc, is_older, run_retention, start_retention, stop_retention,
)
from database.tinydb import TinyDBDatabase
from models.database_tinydb_config import DatabaseTinydbConfig

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
CUT = NOW - timedelta(days=7)


# ── value rules ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, older", [
    ("2026-09-01T00:00:00Z", True),
    ("2026-09-01T00:00:00.123456Z", True),
    ("2026-09-29T11:59:59+00:00", True),
    ("2026-09-29T12:00:01Z", False),
    ("2026-10-05T00:00:00Z", False),
    (datetime(2026, 9, 1), True),                                   # naive = UTC
    (int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp() * 1000), True),   # epoch ms
    (int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()), True),          # epoch s
    (int(NOW.timestamp() * 1000), False),
    ("1693526400000", True),                                        # epoch ms as text
    (None, False), ("", False), ("not a date", False), (True, False),  # unknown -> keep
])
def test_is_older(value, older):
    assert is_older(value, CUT) is older


def test_as_utc_offset_is_normalized():
    assert as_utc("2026-09-29T05:00:00+05:00") == datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("days", [0, -1, 1.5, "7"])
def test_policy_refuses_nonsense_days(days):
    with pytest.raises(ValueError, match="days"):
        RetentionPolicy("log_event", "ts", days)


def test_policy_cutoff():
    assert RetentionPolicy("t", "ts", 7).cutoff(NOW) == CUT


# ── tinydb + the generic default ────────────────────────────────────────────

def _rows():
    return {
        "old1": {"ts": "2026-09-01T00:00:00Z"},
        "old2": {"ts": int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp() * 1000)},
        "new": {"ts": "2026-10-05T00:00:00Z"},
        "null": {"ts": None},
        "junk": {"ts": "yesterday"},
    }


@pytest.fixture
def tiny(tmp_path):
    db = TinyDBDatabase(DatabaseTinydbConfig(name="t", data_dir=str(tmp_path)))
    for key, row in _rows().items():
        db.insert_item("log_event", key, dict(row))
    return db


def test_tinydb_prune(tiny):
    assert tiny.prune_older_than("log_event", "ts", CUT, dry_run=True) == 2
    assert len(tiny.get_all_items("log_event")) == 5                 # dry run deleted nothing
    assert tiny.prune_older_than("log_event", "ts", CUT) == 2
    assert sorted(r["id"] for r in tiny.get_all_items("log_event")) == ["junk", "new", "null"]


def test_tinydb_prune_respects_max_rows(tiny):
    assert tiny.prune_older_than("log_event", "ts", CUT, max_rows=1) == 1
    assert len(tiny.get_all_items("log_event")) == 4


def test_generic_default_on_any_adapter(tiny):
    # the interface default (scan + delete_item), bypassing tinydb's override
    from database.interface import DatabaseAdapter
    assert DatabaseAdapter.prune_older_than(tiny, "log_event", "ts", CUT) == 2
    assert sorted(r["id"] for r in tiny.get_all_items("log_event")) == ["junk", "new", "null"]


def test_run_retention_reports_and_isolates_failures(tiny):
    policies = [RetentionPolicy("log_event", "ts", 7), RetentionPolicy("other", "ts", 7)]

    class Boom:
        def prune_older_than(self, table, *a, **k):
            if table == "other":
                raise RuntimeError("nope")
            return tiny.prune_older_than(table, *a, **k)

    assert run_retention(Boom(), policies, now=NOW) == {"log_event": 2, "other": -1}


# ── postgres (real server) ──────────────────────────────────────────────────

PG_PORT = os.environ.get("RETENTION_TEST_PG_PORT")
pg = pytest.mark.skipif(not PG_PORT, reason="set RETENTION_TEST_PG_PORT to run against a real postgres")


@pytest.fixture
def postgres():
    import psycopg2
    from database.postgres_database import PostgresDatabase
    from models.database_postgres_config import DatabasePostgresConfig
    admin = dict(host="127.0.0.1", port=int(PG_PORT), user="dbuser123",
                 password=os.environ.get("RETENTION_TEST_PG_PASSWORD", "postgres"))
    conn = psycopg2.connect(dbname="postgres", **admin)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("DROP DATABASE IF EXISTS retention_test WITH (FORCE)")
        c.execute("CREATE DATABASE retention_test")
    db = PostgresDatabase(DatabasePostgresConfig(name="rt", database="retention_test", **admin))
    with db._get_cursor() as c:
        c.execute("CREATE TABLE log_event (id text primary key, ts text, ts_tz timestamptz, created bigint, note text)")
        c.execute("CREATE INDEX ON log_event (ts DESC)")
        rows = []
        for i in range(12_000):                       # 10k old, 2k new
            when = (CUT - timedelta(days=1, minutes=i)) if i < 10_000 else (NOW - timedelta(minutes=i - 10_000))
            rows.append((str(uuid.uuid4()), when.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), when,
                         int(when.timestamp() * 1000), "x"))
        rows += [("null", None, None, None, "x"), ("junk", "not a date", None, None, "x"),
                 ("offset-newer", "2026-09-29T15:00:00-05:00", None, None, "x")]   # 20:00Z: after the cutoff
        from psycopg2.extras import execute_values
        execute_values(c, "INSERT INTO log_event VALUES %s", rows)
    yield db
    db.close()
    with conn.cursor() as c:
        c.execute("DROP DATABASE IF EXISTS retention_test WITH (FORCE)")
    conn.close()


@pg
@pytest.mark.parametrize("field", ["ts", "ts_tz", "created"])
def test_postgres_prune_each_column_type(postgres, field):
    assert postgres.prune_older_than("log_event", field, CUT, dry_run=True) == 10_000
    assert postgres.prune_older_than("log_event", field, CUT, batch_size=3000) == 10_000
    left = {r["id"] for r in postgres.query("SELECT id FROM log_event")}
    assert len(left) == 2_003 and {"null", "junk", "offset-newer"} <= left


@pg
def test_postgres_max_rows_and_resume(postgres):
    assert postgres.prune_older_than("log_event", "ts", CUT, batch_size=1000, max_rows=2500) == 2500
    assert postgres.prune_older_than("log_event", "ts", CUT, batch_size=1000) == 7500


@pg
def test_postgres_skips_when_another_process_holds_the_lock(postgres):
    import psycopg2
    other = psycopg2.connect(host="127.0.0.1", port=int(PG_PORT), user="dbuser123", dbname="retention_test",
                             password=os.environ.get("RETENTION_TEST_PG_PASSWORD", "postgres"))
    with other.cursor() as c:
        c.execute("SELECT pg_advisory_lock(hashtext('cloudseeder.retention.log_event'))")
    try:
        assert postgres.prune_older_than("log_event", "ts", CUT) == 0
    finally:
        other.close()
    assert postgres.prune_older_than("log_event", "ts", CUT) == 10_000


@pg
def test_postgres_rejects_bad_identifiers_and_non_time_columns(postgres):
    with pytest.raises(ValueError):
        postgres.prune_older_than("log_event; drop table x", "ts", CUT)
    with pytest.raises(ValueError, match="not a time column"):
        postgres.prune_older_than("log_event", "note", CUT) if False else \
            postgres._older_than_condition(None, "log_event", "note", "boolean", CUT)
    with pytest.raises(ValueError, match="doesn't exist"):
        postgres.prune_older_than("log_event", "nope", CUT)


# ── start_retention / stop_retention ────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_retention_threads():
    yield
    stop_retention()
    for t in retention_module._threads.values():
        t.join(timeout=1)
    retention_module._threads.clear()


def test_start_retention_without_policies_does_nothing(monkeypatch):
    run_mock = MagicMock()
    monkeypatch.setattr(retention_module, "run_retention", run_mock)

    assert start_retention(lambda: None, [], name="no-policies") is None
    run_mock.assert_not_called()


def test_start_retention_runs_in_a_background_thread_and_stops_cleanly(monkeypatch):
    policy = RetentionPolicy(table="job_stats", field="created", days=7)
    ran = threading.Event()
    run_mock = MagicMock(side_effect=lambda *a, **k: ran.set())
    monkeypatch.setattr(retention_module, "run_retention", run_mock)

    thread = start_retention(lambda: object(), [policy], initial_delay_seconds=0, name="runs-once")

    assert ran.wait(timeout=2)
    stop_retention()
    thread.join(timeout=2)
    assert not thread.is_alive()
    run_mock.assert_called_once()


def test_start_retention_is_idempotent_while_the_thread_is_alive(monkeypatch):
    monkeypatch.setattr(retention_module, "run_retention", lambda *a, **k: None)

    first = start_retention(lambda: object(), [RetentionPolicy(table="t", field="f", days=1)],
                             initial_delay_seconds=5, name="idempotent")
    second = start_retention(lambda: object(), [RetentionPolicy(table="t", field="f", days=1)],
                              initial_delay_seconds=5, name="idempotent")

    assert first is second


def test_start_retention_skips_the_run_when_get_db_returns_none(monkeypatch, caplog):
    run_mock = MagicMock()
    monkeypatch.setattr(retention_module, "run_retention", run_mock)
    checked = threading.Event()

    def get_db():
        checked.set()
        return None

    with caplog.at_level("WARNING", logger="database.retention"):
        thread = start_retention(get_db, [RetentionPolicy(table="t", field="f", days=1)],
                                  initial_delay_seconds=0, name="no-db-yet")
        assert checked.wait(timeout=2)
        stop_retention()
        thread.join(timeout=2)

    run_mock.assert_not_called()
    assert "no database client yet" in caplog.text


def test_start_retention_survives_a_run_retention_exception(monkeypatch, caplog):
    attempted = threading.Event()

    def boom(*a, **k):
        attempted.set()
        raise RuntimeError("db down")

    monkeypatch.setattr(retention_module, "run_retention", boom)

    with caplog.at_level("ERROR", logger="database.retention"):
        thread = start_retention(lambda: object(), [RetentionPolicy(table="t", field="f", days=1)],
                                  initial_delay_seconds=0, name="survives-exception")
        assert attempted.wait(timeout=2)
        stop_retention()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert "retention run failed" in caplog.text
