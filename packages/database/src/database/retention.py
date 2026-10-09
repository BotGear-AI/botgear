"""Data retention — keep a table to its last N days, safely, in any app.

Declared per model in an app's DSL (templates/apps/<app>.yml):

    log_event:
      __retention__: {field: ts, days: 7}

which generates the app's managed ``<app>/retention.py`` (its POLICIES) and
starts :func:`start_retention` from the generated ``main.py``. Nothing else to
wire. The pruning itself is one adapter primitive,
``DatabaseAdapter.prune_older_than`` — a generic default every backend gets,
overridden where the engine can do it better (Postgres: index-driven batched
DELETEs under an advisory lock; TinyDB: in-place remove).

Safety rails, in one place so every app gets them:
  * ``days >= 1`` — a policy can never mean "delete everything";
  * a row is deleted only when its field is *provably* older than the
    cutoff — NULL / unparseable values are kept;
  * deletes run in small batches (short locks, no WAL spike) with a per-run
    cap (``max_rows``) — a big backlog drains over a few runs;
  * one pruner per table at a time across replicas (Postgres advisory lock);
  * kill switch: ``<APP>_RETENTION_ENABLED=false`` (checked by the generated
    ``retention.py``) — e.g. on a rehearsal clone;
  * every run logs what it deleted, per table.

Field values understood (whatever the column holds): ISO-8601 strings (``Z``
or an offset), ``datetime``s, and epoch numbers — integers above 1e11 are
epoch **milliseconds** (cloudseeder's convention for ``created``/``modified``),
smaller ones epoch seconds.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

EPOCH_MS_THRESHOLD = 1e11   # 1e11 s is year 5138; 1e11 ms is 1973 — unambiguous for real data


@dataclass(frozen=True)
class RetentionPolicy:
    table: str
    field: str
    days: int
    batch_size: int = 5000
    max_rows: int = 2_000_000   # per run; the rest goes next run

    def __post_init__(self):
        if not isinstance(self.days, int) or self.days < 1:
            raise ValueError(f"retention {self.table}.{self.field}: days must be an integer >= 1, got {self.days!r}")
        if self.batch_size < 1 or self.max_rows < 1:
            raise ValueError(f"retention {self.table}: batch_size and max_rows must be >= 1")

    def cutoff(self, now: Optional[datetime] = None) -> datetime:
        return (now or datetime.now(timezone.utc)) - timedelta(days=self.days)


def as_utc(value: Any) -> Optional[datetime]:
    """A field value as an aware UTC datetime, or None when it isn't a
    recognizable point in time (then the row is kept)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > EPOCH_MS_THRESHOLD else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return as_utc(int(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return as_utc(parsed)
    return None


def is_older(value: Any, cutoff: datetime) -> bool:
    when = as_utc(value)
    return when is not None and when < cutoff


def run_retention(db, policies: Iterable[RetentionPolicy], *, now: Optional[datetime] = None,
                  dry_run: bool = False) -> Dict[str, int]:
    """Apply every policy once. Returns {table: rows deleted (or that would
    be, with dry_run)}; a failing table is logged and reported as -1 without
    stopping the others."""
    results: Dict[str, int] = {}
    for p in policies:
        cutoff = p.cutoff(now)
        try:
            n = db.prune_older_than(p.table, p.field, cutoff, batch_size=p.batch_size,
                                    max_rows=p.max_rows, dry_run=dry_run)
            results[p.table] = n
            logger.info("retention %s: %s %d row(s) with %s older than %s (%d days)", p.table,
                        "would delete" if dry_run else "deleted", n, p.field,
                        cutoff.isoformat(timespec="seconds"), p.days)
        except Exception:
            logger.exception("retention %s: prune failed", p.table)
            results[p.table] = -1
    return results


_threads: Dict[str, threading.Thread] = {}
_stop = threading.Event()


def start_retention(get_db: Callable[[], Any], policies: List[RetentionPolicy], *,
                    interval_hours: float = 6, initial_delay_seconds: float = 300,
                    name: str = "default") -> Optional[threading.Thread]:
    """Run :func:`run_retention` in a daemon thread: first after
    ``initial_delay_seconds`` (keeps startup fast), then every
    ``interval_hours``. ``get_db`` is called each run (the app's
    ``database.factory.get_database_client``). Idempotent per ``name``."""
    if not policies:
        return None
    if name in _threads and _threads[name].is_alive():
        return _threads[name]
    _stop.clear()

    def loop():
        if _stop.wait(initial_delay_seconds):
            return
        while True:
            try:
                db = get_db()
                if db is None:
                    logger.warning("retention: no database client yet — skipping this run")
                else:
                    run_retention(db, policies)
            except Exception:
                logger.exception("retention run failed")
            if _stop.wait(max(60.0, interval_hours * 3600)):
                return

    t = threading.Thread(target=loop, name=f"retention-{name}", daemon=True)
    t.start()
    _threads[name] = t
    logger.info("retention: %s every %sh (first run in %ss)",
                ", ".join(f"{p.table}.{p.field} > {p.days}d" for p in policies),
                interval_hours, int(initial_delay_seconds))
    return t


def stop_retention() -> None:
    _stop.set()
