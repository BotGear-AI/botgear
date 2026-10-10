# managed
"""Simple asynchronous resource monitor.

Package-level utility, shared by every generated app via templates/tpl/ —
this module must stay self-contained and unaware of any specific app (no
app's Pipeline, no app's config/settings shape, no app-specific import).
A caller wires the monitor into its own app by passing plain values
(which paths to watch, an optional callback) rather than this module
reaching into app internals itself.

Provides a POPO ResourceStatus and a monitor coroutine that periodically
samples one or more disk paths plus memory and CPU usage, and logs a
warning when a threshold is exceeded. If a caller wants the snapshot
surfaced somewhere app-specific (e.g. a status endpoint), pass an
`on_snapshot` callback — the monitor has no built-in opinion on where
results go.

Note: this uses psutil if available for more accurate memory/cpu readings. If psutil
is not installed the monitor will still sample disk usage and basic CPU/load values.
"""

import asyncio
import logging
import shutil
import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional

try:
    import psutil
except Exception:
    psutil = None

logger = logging.getLogger(__name__)

# Alert thresholds (fractions of resource used)
alert_disk_space_threshold = 0.85
alert_memory_threshold = 0.90
alert_cpu = 0.95

# Backward-compatible default: a single caller-unspecified disk path means
# "just watch the root filesystem", matching this module's original
# behavior. Callers whose app data lives on a separately mounted volume
# (a common setup) should pass every path worth watching explicitly —
# e.g. `disk_paths=["/", settings.data_dir]` — since "/" being nowhere
# near full says nothing about a separate data volume that is.
DEFAULT_DISK_PATHS = ["/"]


@dataclass
class DiskStatus:
    path: str
    total: int
    free: int

    @property
    def used_fraction(self) -> float:
        if not self.total:
            return 0.0
        return 1.0 - (self.free / self.total)


@dataclass
class ResourceStatus:
    disks: List[DiskStatus] = field(default_factory=list)
    memory_total: int = 0
    memory_free: int = 0
    cpu_count: int = 1
    cpu_percent: float = 0.0
    load_avg: float = 0.0


def resolve_disk_paths(settings: Optional[object] = None, extra_paths: Optional[List[str]] = None) -> List[str]:
    """Best-effort disk_paths list: "/" plus any app-specific paths worth
    watching, deduplicated by resolved absolute path.

    Reads `settings.data_dir` via getattr — safely a no-op for settings
    objects without one — since app data commonly lives on a separately
    mounted volume distinct from "/". This is the single place both the
    background monitor and an on-demand caller (e.g. a status API
    endpoint that wants a fresh reading without needing the monitor loop
    running) should derive paths from, so they never silently disagree
    about what's being watched.
    """
    paths: List[str] = ["/"]
    seen = {os.path.abspath("/")}

    data_dir = getattr(settings, "data_dir", None) if settings is not None else None
    candidates = list(extra_paths or [])
    if data_dir:
        candidates.insert(0, data_dir)

    for p in candidates:
        abs_p = os.path.abspath(p)
        if abs_p not in seen:
            paths.append(p)
            seen.add(abs_p)
    return paths


def sample_disks(paths: List[str]) -> List[DiskStatus]:
    """One-shot synchronous disk usage sample for the given paths. Used by
    `monitor()` internally, and importable directly by any on-demand
    caller that wants a fresh reading without needing the background
    monitor loop running (e.g. a status API endpoint)."""
    disks: List[DiskStatus] = []
    for path in paths:
        try:
            du = shutil.disk_usage(path)
            disks.append(DiskStatus(path=path, total=du.total, free=du.free))
        except Exception as e:
            logger.exception("Error getting disk usage for %s: %s", path, e)
            disks.append(DiskStatus(path=path, total=0, free=0))
    return disks


async def monitor(
    disk_paths: Optional[List[str]] = None,
    interval_seconds: int = 10,
    on_snapshot: Optional[Callable[[ResourceStatus], None]] = None,
):
    """Periodically sample system resources across one or more disk paths.

    The monitor runs forever until cancelled. It is intentionally tolerant of
    missing optional dependencies (psutil) and of any individual disk path
    being unreadable (logged and skipped, doesn't abort the other paths).

    `on_snapshot`, if given, is called with the latest ResourceStatus each
    cycle — this is how a caller attaches the snapshot to whatever
    app-specific place it needs to live. Exceptions raised by the callback
    are logged and otherwise ignored so a bad callback can't kill the
    monitor loop.
    """
    paths = list(disk_paths) if disk_paths else list(DEFAULT_DISK_PATHS)
    logger.info("Starting resource monitor (interval=%ss, disk_paths=%s)", interval_seconds, paths)
    while True:
        try:
            disks = sample_disks(paths)

            # Memory & CPU
            if psutil:
                try:
                    vm = psutil.virtual_memory()
                    mem_total = getattr(vm, "total", 0)
                    mem_free = getattr(vm, "available", 0)
                    cpu_pct = psutil.cpu_percent(interval=None)
                    cpu_count = psutil.cpu_count(logical=True) or (os.cpu_count() or 1)
                except Exception as e:
                    logger.exception("psutil error reading memory/cpu: %s", e)
                    mem_total = 0
                    mem_free = 0
                    cpu_pct = 0.0
                    cpu_count = os.cpu_count() or 1
            else:
                # psutil not available: best-effort fallbacks
                mem_total = 0
                mem_free = 0
                cpu_pct = 0.0
                cpu_count = os.cpu_count() or 1

            # Load average (Unix/macos)
            try:
                load_avg = os.getloadavg()[0]
            except Exception:
                load_avg = 0.0

            status = ResourceStatus(
                disks=disks,
                memory_total=mem_total,
                memory_free=mem_free,
                cpu_count=cpu_count,
                cpu_percent=float(cpu_pct),
                load_avg=float(load_avg),
            )

            # Compute fractions used when possible
            mem_used_frac = 0.0
            cpu_used_frac = status.cpu_percent / 100.0

            if status.memory_total and status.memory_total > 0:
                mem_used_frac = 1.0 - (status.memory_free / status.memory_total)

            # Log warnings if thresholds exceeded — once per watched disk.
            for disk in disks:
                if disk.used_fraction >= alert_disk_space_threshold:
                    logger.warning(
                        "Disk usage high on %s: used=%.2f%% (threshold=%.2f%%)",
                        disk.path, disk.used_fraction * 100.0, alert_disk_space_threshold * 100.0,
                    )

            if mem_used_frac >= alert_memory_threshold:
                logger.warning("Memory usage high: used=%.2f%% (threshold=%.2f%%)", mem_used_frac * 100.0, alert_memory_threshold * 100.0)

            if cpu_used_frac >= alert_cpu:
                logger.warning("CPU intensive: cpu=%.2f%% (threshold=%.2f%%)", cpu_used_frac * 100.0, alert_cpu * 100.0)

            if on_snapshot is not None:
                try:
                    on_snapshot(status)
                except Exception:
                    logger.exception("resource monitor: on_snapshot callback failed")

        except Exception as e:
            logger.exception("Unhandled error in resource monitor: %s", e)

        await asyncio.sleep(interval_seconds)


class ResourceMonitor:
    """Wrapper class providing the same lifecycle API as Pipeline/AppServer.

    Construct and call `await ResourceMonitor.run()` to run until cancelled,
    or `.start()` for a non-blocking launch. Call `await ResourceMonitor.stop()`
    to request shutdown.
    """

    def __init__(
        self,
        settings: Optional[object] = None,
        interval_seconds: int = 10,
        disk_paths: Optional[List[str]] = None,
        on_snapshot: Optional[Callable[[ResourceStatus], None]] = None,
    ):
        # `settings` is accepted (unused internally) only to match the
        # Pipeline(settings)/AppServer(settings) construction convention —
        # this module must not read app-specific fields off it. A caller
        # that wants monitoring scoped to its own data volume should pass
        # that path explicitly via `disk_paths`.
        self.settings = settings
        self.interval_seconds = interval_seconds
        self.disk_paths = disk_paths
        self.on_snapshot = on_snapshot
        self._task: Optional[asyncio.Task] = None

    async def run(self):
        """Start the background monitor task and await it until cancelled."""
        if self._task is not None and not self._task.done():
            # already running
            return
        # create the underlying monitor task and await it
        self._task = asyncio.create_task(
            monitor(self.disk_paths, interval_seconds=self.interval_seconds, on_snapshot=self.on_snapshot)
        )
        try:
            await self._task
        except asyncio.CancelledError:
            # expected on shutdown
            return

    def start(self):
        """Start the background monitor task without awaiting it (non-blocking).

        Use this when you want the checker to run alongside another component and
        not block the current coroutine. This keeps asyncio.create_task calls
        inside the ResourceMonitor implementation rather than exposing them to
        callers.
        """
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                monitor(self.disk_paths, interval_seconds=self.interval_seconds, on_snapshot=self.on_snapshot)
            )

    async def stop(self):
        """Cancel the running monitor task and wait for it to finish."""
        if self._task is None:
            return
        try:
            self._task.cancel()
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
