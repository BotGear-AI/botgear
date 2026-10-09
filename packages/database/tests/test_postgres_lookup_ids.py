# unmanaged
"""Real PostgreSQL compatibility for numeric external IDs and text primary keys.

Marked `integration`: a plain run deselects it (pyproject addopts, and
run_package_tests.py's `-m "not integration"`). Run it against any Postgres
(temporary tables only):
    DATABASE_ID_TEST_DSN=postgresql://... pytest tests/test_postgres_lookup_ids.py -m integration
Selected without DATABASE_ID_TEST_DSN, it fails.
"""
import os
from contextlib import contextmanager

import psycopg2
import pytest
from database.postgres_database import PostgresDatabase


@pytest.mark.integration
@pytest.mark.parametrize('column_type,key,stored', [
    ('varchar', 152652850082699, '152652850082699'),
    ('bigint', 152652850082699, 152652850082699),
    ('varchar', '0000082699', '0000082699'),
])
def test_get_item_preserves_id_with_real_postgres(column_type, key, stored):
    dsn = os.environ.get('DATABASE_ID_TEST_DSN')
    if not dsn:
        pytest.fail('DATABASE_ID_TEST_DSN is not set: point it at any PostgreSQL (temporary tables only)')
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute('CREATE TEMP TABLE lookup_id_probe (id '+column_type+' PRIMARY KEY, label text)')
            cur.execute('INSERT INTO lookup_id_probe VALUES (%s, %s)', (stored, 'original'))
        db = PostgresDatabase.__new__(PostgresDatabase)
        @contextmanager
        def cursor(**kwargs):
            with conn.cursor(**kwargs) as cur:
                yield cur
        db._get_cursor = cursor
        assert db.get_item('lookup_id_probe', key) == {'id': stored, 'label': 'original'}
        assert db.get_item('lookup_id_probe', 42) is None
    finally:
        conn.rollback()
        conn.close()
