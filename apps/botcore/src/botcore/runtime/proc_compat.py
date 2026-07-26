# unmanaged
"""Cross-platform process supervision primitives.

botcore runs each service as its own OS process and *supervises* it:
spawn it in its own group, stop it gracefully (so the service's
``on_stop`` runs — the arduino service quiets Firmata + closes the serial
port there), escalate to a hard kill if it refuses, scan for orphans, and
resolve the per-type venv interpreter. All of that was POSIX-only. This
module is the single seam where the OS-specific bits live so the rest of
the runtime stays platform-agnostic.

Design contract
---------------
* **Linux/macOS is the working production path and MUST NOT change.**
  Every POSIX branch below reproduces the *exact* syscalls the runtime
  used before this module existed (``os.setsid`` process groups,
  ``os.killpg`` SIGTERM→SIGKILL, ``os.kill(pid, 0)`` liveness, the
  ``/proc`` reads for age/cmdline/ppid, the ``/proc`` orphan walk). psutil
  is deliberately **not** called on POSIX — it is used only on the
  ``IS_WINDOWS`` branch — so the Linux orphan-scan results, ages and kills
  are byte-for-byte what they were.

* **The SIGTERM→on_stop graceful-shutdown contract is sacred.** On POSIX
  we keep sending SIGTERM to the process *group* first. On Windows there
  is no SIGTERM; the parallel is: spawn with ``CREATE_NEW_PROCESS_GROUP``
  (group id == child pid) and stop with ``CTRL_BREAK_EVENT``, which the
  child's CRT delivers as ``SIGBREAK`` — the rlx_bus ``SubprocessService``
  base installs a SIGBREAK handler that drives the same ``on_stop`` path.

Group invariant (the linchpin)
------------------------------
``os.killpg(pid, sig)`` only reaps the whole subtree (uvicorn workers,
etc.) because ``os.setsid`` made the child a group leader whose pgid == its
pid. The Windows analogue is ``CREATE_NEW_PROCESS_GROUP`` giving a group id
== child pid that ``CTRL_BREAK_EVENT`` targets. ``spawn_kwargs`` and the
stop-signal choice are a matched pair — keep them consistent.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

import psutil


logger = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"

# Env var a botcore backend stamps on itself (mark_backend_process) so a
# peer runtime can recognize it across a PyInstaller freeze, where the
# 'botcore.main' argv marker disappears. See is_backend_pid.
_BACKEND_MARKER_ENV = "BOTCORE_BACKEND"


# ─── spawn ────────────────────────────────────────────────────────────
def spawn_kwargs() -> Dict[str, object]:
    """Extra ``subprocess.Popen`` kwargs that put the child in its own
    process group so the whole subtree can be signalled at once.

    POSIX: ``start_new_session=True`` — identical effect to the historical
    ``preexec_fn=os.setsid`` (child becomes a session+group leader, so
    ``os.killpg(pid, …)`` reaches it and its descendants) but fork-safe
    (no Python callable runs in the child).

    Windows: ``CREATE_NEW_PROCESS_GROUP`` makes the child the root of a new
    process group whose id == child pid, which is required for
    ``CTRL_BREAK_EVENT`` to be routable to just this subtree.
    ``CREATE_NO_WINDOW`` keeps a GUI-launched backend from popping a
    console per service while still leaving the child a (hidden) console —
    ``DETACHED_PROCESS`` is intentionally NOT used because a child with no
    console at all cannot receive ``GenerateConsoleCtrlEvent``. The
    stdout/stderr PIPEs the caller sets still work under both flags.
    """
    if IS_WINDOWS:
        return {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        }
    return {"start_new_session": True}


# ─── graceful stop / hard kill ────────────────────────────────────────
def _valid_pid(pid: Optional[int]) -> bool:
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0


def terminate_tree(pid: int) -> bool:
    """SIGTERM the process *group* (graceful — lets ``on_stop`` run).

    Returns True when the signal was delivered, False when the target is
    already gone / genuinely unsignalable. This is the bool-returning form
    the reconciler's orphan stats depend on.

    POSIX: ``os.killpg(pid, SIGTERM)`` with a plain ``os.kill`` fallback —
    byte-identical to the historical ``reconciler._signal(pid, SIGTERM)``.

    Windows: ``CTRL_BREAK_EVENT`` to the group (delivered to the child as
    SIGBREAK → ``on_stop``); if that group isn't reachable (e.g. an orphan
    inherited from a dead runtime) fall back to psutil ``terminate()``,
    which is abrupt (no ``on_stop``) — acceptable for orphan reaping.
    """
    if not _valid_pid(pid):
        return False
    if IS_WINDOWS:
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
            return True
        except (ProcessLookupError, OSError):
            # Orphan path (dead-runtime child, no bus channel): abrupt
            # tree-walk kill. A managed live stop goes through the bus and
            # never lands here, so skipping on_stop is acceptable.
            return force_kill_tree(pid)
    try:
        os.killpg(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except ProcessLookupError:
            return False
        except Exception:  # noqa: BLE001
            return False


def force_kill_tree(pid: int) -> bool:
    """SIGKILL the process *group* — the escalation when a prior graceful
    stop was ignored. Returns True on delivery, False when already gone.

    POSIX: ``os.killpg(pid, SIGKILL)`` with an ``os.kill`` fallback — the
    whole group dies in one atomic syscall (matches the historical
    ``reconciler._signal(pid, SIGKILL)`` and ``process_manager`` SIGKILL).

    Windows: there is no atomic group SIGKILL, so walk the tree with psutil
    — snapshot ``children(recursive=True)`` and ``kill()`` each, then the
    root (``TerminateProcess``). Snapshot-then-kill is racy for a
    grandchild spawned mid-enumeration; Job Objects would close that gap
    but are out of scope here.
    """
    if not _valid_pid(pid):
        return False
    if IS_WINDOWS:
        try:
            p = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return False
        try:
            kids = p.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            kids = []
        for child in kids:
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        try:
            p.kill()
            return True
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return False
    try:
        os.killpg(pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except ProcessLookupError:
            return False
        except Exception:  # noqa: BLE001
            return False


def request_graceful_stop(pid: int) -> None:
    """Ask ``pid``'s group to shut down cleanly — and NEVER hard-kill.

    This is the primitive behind a *managed* stop, where an abrupt kill
    would skip ``on_stop`` (e.g. the arduino serial-port close). Errors are
    swallowed; a caller that needs a delivered/gone answer uses
    :func:`terminate_tree` instead.

    POSIX: ``os.killpg(pid, SIGTERM)`` with an ``os.kill`` fallback —
    byte-identical to the historical graceful stop (the reconciler
    ``_signal(pid, SIGTERM)`` / ``discovery._kill`` / ``script_runner``
    SIGTERM leg).

    Windows: best-effort ``CTRL_BREAK_EVENT``. It only lands if the child
    shares our console (usually it won't — services spawn with
    ``CREATE_NO_WINDOW``), so on Windows the authoritative graceful channel
    is the bus shutdown request from ``process_manager.stop``. This call
    does NOT fall back to an abrupt kill — doing so would defeat the
    graceful contract for a live service (FINDING-2).
    """
    if not _valid_pid(pid):
        return
    if IS_WINDOWS:
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        except (ProcessLookupError, OSError):
            pass
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except (PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def snapshot_child_pids(pid: int) -> List[int]:
    """Recursive child pids of ``pid`` right now — Windows only.

    ``process_manager.stop`` snapshots these BEFORE requesting a graceful
    stop so it can sweep any workers the service failed to reap itself:
    Windows has no atomic process-group kill and does not auto-reap
    orphaned children. POSIX reaps the whole group via ``killpg``, so this
    returns ``[]`` there (and never touches psutil, per the module
    contract). A Job Object would make the sweep unnecessary — see
    docs/TODO_WINDOWS_SUPPORT.md.
    """
    if not IS_WINDOWS or not _valid_pid(pid):
        return []
    try:
        return [c.pid for c in psutil.Process(pid).children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []


# ─── liveness ─────────────────────────────────────────────────────────
def pid_alive(pid: Optional[int]) -> bool:
    """Cheap liveness check for an OS pid. None / non-int / <= 0 → False.

    POSIX: ``os.kill(pid, 0)`` — byte-identical to the historical probe,
    including ``PermissionError`` → alive (exists but not ours) and any
    other ``OSError`` → dead.

    Windows: ``psutil.pid_exists`` (also returns True for a process we
    can't signal, preserving the PermissionError-means-alive semantics).
    """
    if not _valid_pid(pid):
        return False
    if IS_WINDOWS:
        try:
            return psutil.pid_exists(pid)
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it. Still alive from our pov.
        return True
    except OSError:
        return False
    return True


# ─── introspection (age / cmdline / ppid / enumeration) ───────────────
# POSIX branches reproduce the exact /proc reads the runtime used before,
# so the reconciler's orphan-scan results, ages and cmdlines are unchanged.
def proc_age_s(pid: int) -> float:
    """Seconds the process has been alive. 0.0 on any failure — an
    unreadable process is deliberately treated as "too young to reap" by
    the reconciler's grace window (fail-safe: never kill what we can't
    introspect)."""
    if IS_WINDOWS:
        try:
            return max(0.0, time.time() - psutil.Process(pid).create_time())
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            return 0.0
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().split()
        # Field 22 (index 21) is starttime in clock ticks since boot.
        starttime_ticks = int(parts[21])
        clk_tck = os.sysconf("SC_CLK_TCK") or 100
        with open("/proc/uptime") as f:
            uptime_s = float(f.read().split()[0])
        return uptime_s - (starttime_ticks / clk_tck)
    except Exception:  # noqa: BLE001
        return 0.0


def proc_cmdline(pid: int) -> Optional[str]:
    """The process command line, space-joined, or None if unreadable/empty.
    Used for substring matching (``"_service" in cmd`` etc.)."""
    if IS_WINDOWS:
        try:
            argv = psutil.Process(pid).cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None
        return " ".join(argv) if argv else None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            data = f.read()
    except (OSError, PermissionError):
        return None
    if not data:
        return None
    return data.decode("utf-8", "replace").replace("\x00", " ")


def proc_argv(pid: int) -> Optional[List[str]]:
    """The process command line as an argv list, or None if unreadable/empty."""
    if IS_WINDOWS:
        try:
            argv = psutil.Process(pid).cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None
        return list(argv) if argv else None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            data = f.read()
    except (OSError, PermissionError):
        return None
    if not data:
        return None
    argv = [a for a in data.decode("utf-8", "replace").split("\x00") if a]
    return argv or None


def proc_ppid(pid: int) -> Optional[int]:
    """Parent pid, or None if unreadable."""
    if IS_WINDOWS:
        try:
            return psutil.Process(pid).ppid()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None
    try:
        with open(f"/proc/{pid}/stat") as f:
            # The comm field can contain spaces + parens; rsplit on ") "
            # skips past it. ppid is then field 1 of the tail.
            stat_fields = f.read().rsplit(") ", 1)[-1].split()
        return int(stat_fields[1])
    except (OSError, ValueError, IndexError):
        return None


def iter_pids() -> Iterator[int]:
    """Yield the pid of every process currently visible to us.

    POSIX: enumerate ``/proc`` exactly as before. Windows: psutil. On an
    unreadable ``/proc`` (shouldn't happen) yields nothing, matching the
    historical scan's empty-list fallback."""
    if IS_WINDOWS:
        for p in psutil.process_iter():
            yield p.pid
        return
    try:
        entries = os.listdir("/proc")
    except OSError:
        return
    for entry in entries:
        if entry.isdigit():
            yield int(entry)


def iter_service_subprocesses(
    predicate: Callable[[str], bool],
) -> Iterator[Dict[str, object]]:
    """Yield ``{pid, cmdline, argv, ppid}`` for every visible process whose
    space-joined command line satisfies ``predicate``.

    Cross-platform replacement for a ``/proc`` walk. Callers keep their own
    matcher (the ``predicate``) and their own ownership policy (mine vs
    other-live-runtime vs orphan) on top of the yielded records — this
    helper only centralizes enumeration + the read-and-skip-on-failure
    fail-safe. Processes that die mid-scan or that we can't read are
    silently skipped."""
    for pid in iter_pids():
        cmd = proc_cmdline(pid)
        if not cmd:
            continue
        if not predicate(cmd):
            continue
        yield {
            "pid": pid,
            "cmdline": cmd,
            "argv": proc_argv(pid) or [],
            "ppid": proc_ppid(pid),
        }


# ─── backend identity (freeze-proof cross-runtime recognition) ────────
def mark_backend_process() -> None:
    """Stamp THIS process as a botcore backend so a peer runtime can
    recognize it across a PyInstaller freeze (where the ``botcore.main``
    argv marker disappears — the frozen cmdline is ``.../botcore.exe``).
    Call once at backend startup.

    On a source build this is inert wrt the reconciler (which still matches
    the argv marker via :func:`is_backend_pid`), so the POSIX production
    path is unchanged. The value is our own pid, which lets
    :func:`is_backend_pid` distinguish a real backend from a subprocess
    that merely *inherited* this env var.
    """
    os.environ[_BACKEND_MARKER_ENV] = str(os.getpid())


def is_backend_pid(pid: int) -> bool:
    """True when ``pid`` is a botcore backend (this runtime or a live
    peer). The reconciler uses this to avoid reaping another live runtime's
    healthy subprocesses (cross-runtime fratricide).

    Source builds: identified by the ``botcore.main`` argv marker — the
    byte-identical substring check the reconciler used before, so the POSIX
    production path is unchanged and psutil is NOT consulted.

    Frozen builds (PyInstaller, Windows OR Linux): the argv marker is gone,
    so fall back to the env marker set by :func:`mark_backend_process`,
    read from the candidate via psutil. A backend stamps
    ``BOTCORE_BACKEND=<its-own-pid>``; a subprocess child merely
    inherits the *backend's* pid value, so ``env[...] == str(child_pid)``
    is False and the child is correctly NOT treated as a backend.
    """
    cmd = proc_cmdline(pid)
    if cmd and "botcore.main" in cmd:
        return True
    if not getattr(sys, "frozen", False):
        return False
    try:
        env = psutil.Process(pid).environ()
    except Exception:  # noqa: BLE001 — environ() can raise AccessDenied/NoSuchProcess
        return False
    return env.get(_BACKEND_MARKER_ENV) == str(pid)


# ─── venv layout ──────────────────────────────────────────────────────
def venv_bin_dir(venv: Path) -> Path:
    """The venv's executables directory: ``<venv>/bin`` on POSIX,
    ``<venv>/Scripts`` on Windows. Single source of truth mirroring
    ``installer._bundled_uv_path``'s nt-aware style."""
    return venv / ("Scripts" if IS_WINDOWS else "bin")


def venv_python(venv: Path) -> Path:
    """The venv interpreter given the venv ROOT: ``<venv>/bin/python`` on
    POSIX, ``<venv>/Scripts/python.exe`` on Windows."""
    return venv_bin_dir(venv) / ("python.exe" if IS_WINDOWS else "python")


def venv_python_in(bin_dir: Path) -> Path:
    """The venv interpreter given the venv's BIN dir directly:
    ``<bin_dir>/python`` on POSIX, ``<bin_dir>/python.exe`` on Windows.
    Preferred over ``venv_python(bin_dir.parent)`` when the caller already
    holds the bin dir — it does not assume the bin dir is named ``bin``."""
    return bin_dir / ("python.exe" if IS_WINDOWS else "python")
