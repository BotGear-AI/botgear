# TODO — Windows support (build + runtime)

Design for shipping a Windows distributable of botcore and making it
actually run. **Planning doc — nothing here is implemented yet.** Companion to
`packaging/build.sh` + the `package_botcore` workflow (which today ships
`linux-x86_64`, `linux-aarch64`, and `darwin-arm64`).

## TL;DR

Windows is a genuine port, not a build-flag. It splits into two independently
shippable layers:

- **Layer A — Build**: produce `botcore.exe` + a `.zip` in CI. Small: a
  Windows `uv` target in `build.sh`, zip packaging, and a `build-windows` job.
  The backend boots and answers `/v1/version` after this — enough to publish an
  artifact — but it can't yet spawn/stop subprocess services.
- **Layer B — Runtime process management**: botcore spawns each service as a
  child process and manages it via **POSIX process groups** (`os.setsid` +
  `os.killpg`) and **`/proc`** introspection. None of that exists on Windows.
  This is the real work: a small cross-platform process-compat module (built on
  the already-present **`psutil`**) + preserving graceful shutdown.

Already portable (verified, no work needed): `_bundled_uv_path()` already picks
`uv.exe` on `os.name=='nt'` (`installer.py:95`); SSL cert gen is pure-Python
`cryptography`, not the `openssl` CLI (`auth/generate_ssl.py`); `os.pathsep` +
pyserial `comports()` are already used; no hardcoded `/tmp` in the app.

---

## Layer A — Build a Windows distributable

Runner: **GitHub-hosted `windows-latest`** (Git Bash ships with it — bash +
coreutils incl. `sha256sum`, plus `tar` and `curl`), so `build.sh` runs under
`shell: bash` almost unchanged. Changes:

1. **`build.sh` — add a Windows `uv` target** (the `uname` case at ~line 168
   currently `exit 1`s on anything non-Linux/Darwin). Add:
   ```sh
   MINGW*|MSYS*|CYGWIN*|Windows_NT-*)  # uname -s under Git Bash = MINGW64_NT-...
       UV_TARGET="x86_64-pc-windows-msvc"; UV_EXT="zip"; UV_BIN="uv.exe" ;;
   ```
   The uv Windows asset is a **`.zip`** containing `uv.exe` (not `tar.gz`/`uv`),
   so the extract + flatten + `cp … dist/botcore/uv` step needs a `.zip`
   branch that lands `uv.exe`. `chmod +x` is a harmless no-op under Git Bash.
2. **PLATFORM_TAG** — `uname -s -m` under Git Bash yields `mingw64_nt-...-x86_64`;
   force `PLATFORM_TAG=windows-x86_64` for that case (or set it in the workflow
   `env`) so the artifact is `botcore-<ver>-windows-x86_64`.
3. **Packaging → `.zip`, not `.tar.gz`.** Windows users expect zip and the
   double-click story is better. Gate on platform: `zip -r` (Git Bash has it) or
   PowerShell `Compress-Archive` on Windows; keep `tar -czf` on POSIX. The
   `sha256_file` helper is already portable (added for macOS).
4. **PyInstaller spec** — `packaging/botcore.spec` is already
   platform-neutral (`collect_all`/`collect_submodules`, relative `_internal/ui`
   data); PyInstaller emits `botcore.exe` on Windows automatically. Verify no
   POSIX-only hidden imports surface at build (e.g. `fcntl`); add Windows-only
   excludes/includes if PyInstaller complains.
5. **Smoke test** — `build.sh`'s smoke starts the binary + curls `/v1/version`.
   Under Git Bash `./dist/botcore/botcore.exe &` works; the check only
   exercises the **backend boot**, not subprocess services, so it passes before
   Layer B lands. Keep it.
6. **CI job** `build-windows` (mirror `build-macos`): `runs-on: windows-latest`,
   `defaults.run.shell: bash`, install `uv` via the PS1 installer (or the bash
   installer under Git Bash), `setup-node`, `./packaging/build.sh`, publish the
   `.zip` + `.sha256`. Gate it to release dispatch like macOS (GH-hosted Windows
   bills ~2× Linux). Shares `github.run_number` → same `0.1.<run>` release.

**Deliverable:** `botcore-<ver>-windows-x86_64.zip` on the release, boots +
serves `/v1/version`. Not yet able to run services — that's Layer B.

---

## Layer B — Runtime process management (the real port)

### The problem

botcore runs each service (`arduino`, `oscope`, …) as its own OS process and
supervises it. The supervisor is POSIX-only in five places:

| File | POSIX construct | Why |
|---|---|---|
| `runtime/process_manager.py:285` | `preexec_fn=os.setsid` | put child in a new process group so its whole tree can be killed together |
| `runtime/process_manager.py:352,365` | `os.killpg(pid, SIGTERM/SIGKILL)` | graceful-then-hard stop of the group |
| `runtime/process_manager.py:194` | `os.kill(pid, 0)` | is-alive check |
| `runtime/reconciler.py:72-101,143-168` | reads `/proc/<pid>/{stat,cmdline}` | age / cmdline / ppid for orphan scan |
| `runtime/reconciler.py:189-213` | `killpg` / `os.kill` / `SIGKILL` | reap orphaned service subprocesses |
| `runtime/discovery.py:120-130` | `killpg` / `os.kill` / `SIGTERM` | SIGTERM an adopted/orphan pid |
| `runtime/script_runner.py:135-159` | `setsid` / `killpg` / `SIGTERM/KILL` | same, for the `python` script service |
| `runtime/lifecycle.py:265` + `installer.py` | `.venv/bin` | venv layout — Windows uses `.venv/Scripts` + `python.exe` |
| `tools/state.py:52` | `os.kill(pid, 0)` | is-alive check |

None of `os.setsid`, `os.killpg`, `signal.SIGKILL`, `preexec_fn`, or `/proc`
exist on Windows.

### The design: one `runtime/proc_compat.py` on top of `psutil`

`psutil` (already a dep) gives all the introspection + tree ops cross-platform.
Centralize every platform difference behind one module; callers stop touching
`os.setsid`/`killpg`/`/proc` directly.

```python
# runtime/proc_compat.py
IS_WINDOWS = os.name == "nt"

def spawn_kwargs() -> dict:
    """Popen kwargs that isolate the child's tree for later group-kill."""
    if IS_WINDOWS:
        # New process group so we can send CTRL_BREAK for a graceful stop;
        # CREATE_NO_WINDOW so services don't pop console windows.
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
                                  | 0x08000000}  # CREATE_NO_WINDOW
    return {"start_new_session": True}   # == preexec_fn=os.setsid, but pickle-safe

def request_graceful_stop(pid: int) -> None:
    """POSIX: SIGTERM the group. Windows: CTRL_BREAK_EVENT to the group."""
    if IS_WINDOWS:
        os.kill(pid, signal.CTRL_BREAK_EVENT)      # child must handle SIGBREAK
    else:
        os.killpg(pid, signal.SIGTERM)

def kill_tree(pid: int, grace_s: float) -> Optional[int]:
    """Graceful stop, wait grace_s, then hard-kill the whole tree. Cross-platform
    via psutil children(recursive=True)."""
    try:
        p = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return None
    kids = p.children(recursive=True)
    request_graceful_stop(pid)
    gone, alive = psutil.wait_procs([p, *kids], timeout=grace_s)
    for survivor in alive:
        survivor.kill()          # TerminateProcess / SIGKILL
    return p.returncode if hasattr(p, "returncode") else None

def pid_alive(pid: int) -> bool:
    return psutil.pid_exists(pid)

def proc_age_s(pid) / proc_cmdline(pid) / proc_ppid(pid):   # psutil, replaces /proc
def iter_service_subprocesses(match) -> list[dict]:          # psutil.process_iter

def venv_bin_dir(venv: Path) -> Path:
    return venv / ("Scripts" if IS_WINDOWS else "bin")
def venv_python(venv: Path) -> Path:
    return venv_bin_dir(venv) / ("python.exe" if IS_WINDOWS else "python")
```

Then rewire:
- `process_manager.start`: `**proc_compat.spawn_kwargs()` instead of
  `preexec_fn=os.setsid`; resolve the venv python via `proc_compat.venv_python`.
- `process_manager.stop`: `proc_compat.kill_tree(pid, grace)` replaces the
  `killpg(SIGTERM)→wait→killpg(SIGKILL)` block.
- `reconciler`: `_process_age_seconds`/`_read_cmdline`/`_read_ppid`/
  `_scan_service_subprocesses` → the `psutil` helpers; `_signal`/`terminate`/
  `kill` → `proc_compat`.
- `discovery`, `script_runner`, `tools/state.py`, `lifecycle.py:265` → the compat
  helpers.

### Preserving graceful shutdown (important — services rely on it)

POSIX services get **SIGTERM → run `on_stop()`** (e.g. the arduino service
quiets its Firmata stream before closing the port — see
`repo/arduino/1.0.0`). Windows has no SIGTERM for GUI/non-console apps; a bare
`TerminateProcess` is `SIGKILL`-equivalent and skips `on_stop`.

Fix: spawn services with `CREATE_NEW_PROCESS_GROUP` (above) and, in the
**`rlx_bus.SubprocessService` base class**, install a `SIGBREAK` handler on
Windows alongside the existing `SIGTERM` handler — both set `_stop_event` and run
`on_stop`. Then `request_graceful_stop` sends `CTRL_BREAK_EVENT`, the child runs
`on_stop`, and `kill_tree` only hard-kills if it overruns the grace window. This
keeps the graceful-shutdown contract identical on both OSes.

### Windows process-tree robustness (optional hardening)

`psutil.children(recursive=True)` tree-kill is race-prone if the parent dies
before it reaps (orphans). If that bites in practice, assign each child to a
**Win32 Job Object** at spawn with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, so the
OS tears down the whole tree when the handle closes — needs `pywin32` or a small
`ctypes` shim. Recommend shipping psutil-tree-kill first; add the Job Object only
if orphaning shows up in Windows e2e.

---

## Layer C — Misc compatibility

- **Serial ownership**: `packages/rlx_bus/serial_ports.py` enumerates via
  `comports()` (already Windows-aware, returns `COM3`) but the *holder/ownership*
  detection is `lsof` over `/dev/tty*` (`serial_ports.py:88`). On Windows,
  degrade gracefully: enumeration works, ownership = empty/unknown (skip the
  `lsof` path when `os.name=='nt'`). The `bus:serial` single-owner model and the
  arduino service (pyserial on a `COM` port) still work.
- **Paths**: audit for any remaining `/`-joined literals; the codebase is largely
  `pathlib`. DATA_DIR is relative + `pathlib`, fine.
- **mDNS/zeroconf**, **SSL**, **uv venvs**: cross-platform already.

---

## Phasing & acceptance

1. **Phase 1 — Build (small).** `build.sh` Windows `uv` target + zip packaging +
   `build-windows` CI job (release-gated). ✅ when a `windows-x86_64.zip` publishes
   and `botcore.exe` answers `/v1/version` in the CI smoke.
2. **Phase 2 — Runtime port (the work).** `proc_compat.py` + rewire the 6 files +
   `SIGBREAK` in the base class. ✅ when, on a real Windows box, you can install +
   start + gracefully stop a service (verify `on_stop` ran, no orphaned
   `python.exe`).
3. **Phase 3 — Hardening.** Serial-ownership degrade; real Windows e2e (arduino
   on a COM-port Arduino + oscope + graceful restart); code-signing.

**Testing:** GH-hosted `windows-latest` covers CI build + boot smoke, but — like
"QEMU proves the arch, a real Pi is the proof" — **a real Windows box (or VM)**
is required to validate Layer B (process groups, COM serial, `on_stop`).

## Open decisions / risks

- **SmartScreen + Defender**: an unsigned PyInstaller `.exe` triggers SmartScreen
  "unrecognized app" and PyInstaller bootloaders are a known Defender
  false-positive. Mitigation = **Authenticode signing** (EV cert ≈ instant
  reputation, ~$300+/yr; standard cert builds reputation over time). Separate,
  paid, analogous to macOS notarization.
- **arm64 Windows** (Surface, etc.) — out of scope; `windows-latest` is x64.
- **Job Object vs psutil tree-kill** — start psutil; escalate only if needed.
- **`.zip` vs keep `.tar.gz`** — recommend `.zip` for Windows UX; both are easy.
- **Long paths / MAX_PATH** — deep `repo/<svc>/.venv/Lib/site-packages/…` trees
  can exceed 260 chars; may need the long-path manifest/registry opt-in. Watch for
  it in Phase 2.
