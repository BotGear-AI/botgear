#!/usr/bin/env python
"""One-shot migration: copy every ``data/databases/<table>.json`` (TinyDB /
lowdb) row into a sqlite file at ``data/databases/botgear.db``.

Source JSON files are left untouched unless ``--commit`` is passed, in
which case each migrated ``<table>.json`` is renamed to ``<table>.json.bak``
as a backup — never deleted. Safe to re-run: uses upsert_item, so a
partial or repeated run just re-copies the same rows.

Why: lowdb/TinyDB corrupts the whole table on an interrupted write;
sqlite doesn't.

Usage (from apps/botgear):

    .venv/bin/python scripts/migrate_to_sqlite.py                 # dry run, reports counts
    .venv/bin/python scripts/migrate_to_sqlite.py --commit         # migrates + backs up JSON
    .venv/bin/python scripts/migrate_to_sqlite.py --data-dir data  # explicit data dir (default: data)

After a verified migration, set BOTGEAR_DATABASE_TYPE=sqlite in .env to
switch the running app over.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packages" / "database" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packages" / "models" / "src"))

from database.factory import create_database_client, get_database_client  # noqa: E402
from database.sqlite_database import SqliteDatabase  # noqa: E402
from database.tinydb import TinyDBDatabase  # noqa: E402
from models.database_sqlite_config import DatabaseSqliteConfig  # noqa: E402
from models.database_tinydb_config import DatabaseTinydbConfig  # noqa: E402
from models.auth_session import AuthSession  # noqa: E402

from botgear.models.api_client import ApiClient  # noqa: E402
from botgear.models.config import Config as BotgearConfig  # noqa: E402
from botgear.models.registration import Registration  # noqa: E402
from botgear.models.service_config import ServiceConfig  # noqa: E402
from botgear.models.service_meta import ServiceMeta  # noqa: E402
from botgear.models.service_proxy import ServiceProxy  # noqa: E402
from botgear.models.service_request import ServiceRequest  # noqa: E402
from botgear.models.user import User  # noqa: E402
from botgear.models.workspace import Workspace  # noqa: E402

# Mirrors event_handlers.on_startup's ensure_table() calls exactly — keep in sync.
TABLE_MODELS = {
    "auth_session": AuthSession,
    "registration": Registration,
    "user": User,
    "service_meta": ServiceMeta,
    "service_proxy": ServiceProxy,
    "service_config": ServiceConfig,
    "service_request": ServiceRequest,
    "workspace": Workspace,
    "api_client": ApiClient,
    "config": BotgearConfig,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data", help="Per-process data dir (default: data)")
    parser.add_argument("--commit", action="store_true", help="Actually write + back up JSON (default: dry run)")
    args = parser.parse_args()

    databases_dir = Path(args.data_dir) / "databases"
    if not databases_dir.is_dir():
        print(f"no {databases_dir} (run from apps/botgear, or pass --data-dir)", file=sys.stderr)
        return 1

    sqlite_path = databases_dir / "botgear.db"

    SqliteDatabase._instances.clear()
    create_database_client(DatabaseTinydbConfig(name="migrate-src", data_dir=str(databases_dir)))
    src = get_database_client("migrate-src")

    # SqliteDatabase.__init__ creates the file + connects as a side effect —
    # do not construct it (or call ensure_table) on a dry run, or a
    # dry-run-only invocation leaves a stray empty botgear.db behind.
    dst = None
    if args.commit:
        create_database_client(DatabaseSqliteConfig(name="migrate-dst", sqlite_path=str(sqlite_path)))
        dst = get_database_client("migrate-dst")
        for model in TABLE_MODELS.values():
            dst.ensure_table(model=model)

    mode = "COMMIT" if args.commit else "DRY RUN"
    print(f"migrating {databases_dir} -> {sqlite_path}  [{mode}]")

    migrated_json_files = []
    any_mismatch = False

    for table, _model in TABLE_MODELS.items():
        rows = src.get_all_items(table)
        if args.commit:
            for row in rows:
                key = row.get("id")
                if key is None:
                    print(f"  {table}: SKIPPING row with no 'id': {row}", file=sys.stderr)
                    continue
                dst.upsert_item(table, key, row)

        dst_count = len(dst.get_all_items(table)) if args.commit else None
        status = f"-> {dst_count} in sqlite" if args.commit else "(dry run, not written)"
        print(f"  {table:16} {len(rows):5} rows  {status}")

        if args.commit and dst_count != len(rows):
            print(f"  {table}: MISMATCH — {len(rows)} source rows but {dst_count} in sqlite", file=sys.stderr)
            any_mismatch = True

        json_file = databases_dir / f"{table}.json"
        if json_file.is_file():
            migrated_json_files.append(json_file)

    if not args.commit:
        print("\nDry run only — re-run with --commit to write sqlite + back up JSON.")
        return 0

    if any_mismatch:
        print("\nRow-count mismatch on at least one table — NOT backing up JSON. Investigate before re-running.", file=sys.stderr)
        return 1

    for json_file in migrated_json_files:
        backup = json_file.with_suffix(json_file.suffix + ".bak")
        json_file.rename(backup)
        print(f"  backed up {json_file.name} -> {backup.name}")

    print("\nOK — migrated. Set BOTGEAR_DATABASE_TYPE=sqlite in .env to switch over.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
