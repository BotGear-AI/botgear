#!/usr/bin/env python3
# unmanaged
"""Release smoke test — run on each platform to check a published botgear release.

Downloads this platform's bundle from a GitHub Release, verifies its
sha256, unpacks it into a temp dir, boots it on a spare port with a fresh
data dir, and drives the real API through the first-run path:

  1. download + sha256 verify          6. load echo_http from the public catalog
  2. unpack                            7. install it (per-type venv via the bundled uv)
  3. boot → /v1/version == release     8. create + start an instance → running
  4. claim the first admin account     9. update check answers
  5. catalog reachable, has echo_http

``echo_http`` is used because it's published to the catalog but NOT bundled,
so step 6-8 exercise the download → verify → venv → spawn path for real.

Standard library only (Python 3.9+). Usage:

    python3 release_smoke.py --version 0.2.0
    python3 release_smoke.py --version 0.2.0 --bundle ~/Downloads/botgear-0.2.0-darwin-arm64.tar.gz
    python3 release_smoke.py --version 0.2.0 --keep      # leave the unpacked install for poking at

Prints one line per step and a JSON summary at the end (paste it into the
release tracker). Exit code 0 only when every step passes.

Not covered here (check by hand): macOS Gatekeeper / Windows SmartScreen on a
browser-downloaded file — this script's download carries no quarantine flag.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SERVICE = "echo_http"
SERVICE_VERSION = "1.0.0"
UA = "botgear-release-smoke"
_INSECURE = ssl.create_default_context()
_INSECURE.check_hostname = False
_INSECURE.verify_mode = ssl.CERT_NONE  # the runtime serves a fresh self-signed cert


def platform_tag() -> str:
    """Same as packaging/build.sh's PLATFORM_TAG and updater.platform_tag()."""
    osname = platform.system().lower()
    arch = platform.machine().lower()
    if osname == "windows" and arch == "amd64":
        arch = "x86_64"
    return f"{osname}-{arch}"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Api:
    def __init__(self, port: int) -> None:
        self.base = f"https://127.0.0.1:{port}"
        self.token: Optional[str] = None

    def call(self, method: str, path: str, body: Optional[dict] = None, timeout: float = 30) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_INSECURE) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"{method} {path} → HTTP {exc.code}: {detail}") from None
        return json.loads(raw) if raw else None


def fetch(url: str, dest: Optional[Path] = None, timeout: float = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if dest is None:
            return resp.read()
        with open(dest, "wb") as fh:
            shutil.copyfileobj(resp, fh)
    return b""


def wait_for(what: str, probe: Callable[[], Any], timeout: float, every: float = 2.0) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = probe()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001 — keep polling until the deadline
            last = exc
        time.sleep(every)
    raise RuntimeError(f"timed out after {timeout:.0f}s waiting for {what} (last: {last!r})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--version", required=True, help="release version, e.g. 0.2.0")
    ap.add_argument("--repo", default="BotGear-AI/botgear")
    ap.add_argument("--bundle", help="test this local archive instead of downloading it")
    ap.add_argument("--port", type=int, default=0, help="port to boot on (default: a free one)")
    ap.add_argument("--keep", action="store_true", help="keep the unpacked install afterwards")
    args = ap.parse_args()

    ptag = platform_tag()
    ext = "zip" if ptag.startswith("windows-") else "tar.gz"
    asset = f"botgear-{args.version}-{ptag}.{ext}"
    work = Path(tempfile.mkdtemp(prefix="botgear-smoke-"))
    port = args.port or free_port()
    api = Api(port)
    results: List[Dict[str, Any]] = []
    proc: Optional[subprocess.Popen] = None
    log_path = work / "botgear.log"
    state: Dict[str, Any] = {}

    def step(name: str, fn: Callable[[], str]) -> bool:
        t0 = time.monotonic()
        try:
            detail = fn() or ""
            ok = True
        except Exception as exc:  # noqa: BLE001 — report and stop
            detail, ok = str(exc), False
        secs = round(time.monotonic() - t0, 1)
        results.append({"step": name, "ok": ok, "seconds": secs, "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'}  {name:<28} {secs:>6.1f}s  {detail}", flush=True)
        return ok

    def s_download() -> str:
        archive = work / asset
        if args.bundle:
            shutil.copy2(Path(args.bundle).expanduser(), archive)
            src = "local file"
        else:
            base = f"https://github.com/{args.repo}/releases/download/{args.version}/{asset}"
            fetch(base, archive)
            state["sha_line"] = fetch(base + ".sha256").decode().strip()
            src = base
        if "sha_line" in state:
            want = state["sha_line"].split()[0].lower()
            got = hashlib.sha256(archive.read_bytes()).hexdigest()
            if got != want:
                raise RuntimeError(f"sha256 mismatch: expected {want[:16]}…, got {got[:16]}…")
            sha_note = f"sha256 {got[:12]}… verified"
        else:
            sha_note = "sha256 not checked (--bundle)"
        state["archive"] = archive
        return f"{asset} ({archive.stat().st_size // (1 << 20)} MB) from {src}; {sha_note}"

    def s_unpack() -> str:
        dest = work / "install"
        archive = state["archive"]
        if ext == "zip":
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(dest)
        else:
            with tarfile.open(archive) as tf:
                tf.extractall(dest)  # noqa: S202 — our own release artifact, sha256-verified above
        exe = "botgear.exe" if ext == "zip" else "botgear"
        found = [p for p in dest.rglob(exe) if p.is_file()]
        if not found:
            raise RuntimeError(f"no {exe} in the archive")
        binary = min(found, key=lambda p: len(p.parts))
        if ext != "zip":
            binary.chmod(binary.stat().st_mode | 0o111)
        state["binary"] = binary
        return str(binary.relative_to(work))

    def s_boot() -> str:
        nonlocal proc
        binary: Path = state["binary"]
        env = dict(os.environ, BOTGEAR_PORT=str(port))
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0  # type: ignore[attr-defined]
        log = open(log_path, "wb")
        proc = subprocess.Popen([str(binary)], cwd=binary.parent, env=env, stdout=log,
                                stderr=subprocess.STDOUT, creationflags=flags)
        def probe() -> Any:
            # HTTPS is the default since ssl_enabled flipped on; older
            # bundles (and BOTGEAR_SSL_ENABLED=False) serve plain HTTP.
            for scheme in ("https", "http"):
                api.base = f"{scheme}://127.0.0.1:{port}"
                try:
                    return api.call("GET", "/v1/version", timeout=5)
                except (urllib.error.URLError, ssl.SSLError, ConnectionError, RuntimeError):
                    continue
            return None

        ver = wait_for("/v1/version", probe, timeout=120)
        got = ver.get("version") if isinstance(ver, dict) else ver
        if got != args.version:
            raise RuntimeError(f"/v1/version says {got!r}, expected {args.version!r}")
        return f"{api.base}, version {got}"

    def s_claim() -> str:
        if api.call("GET", "/v1/auth/has-users").get("exists"):
            raise RuntimeError("fresh install already has a user")
        email = f"smoke-{secrets.token_hex(3)}@example.com"
        res = api.call("POST", "/v1/auth/claim-first-user",
                       {"email": email, "password": secrets.token_urlsafe(16)})
        api.token = res["access_token"]
        return f"admin {email}"

    def s_catalog() -> str:
        cat = api.call("GET", "/v1/registry/catalog", timeout=60)
        svc = next((s for s in cat.get("services", []) if s.get("name") == SERVICE), None)
        if not svc:
            raise RuntimeError(f"{SERVICE} not in catalog {cat.get('registry_url')}")
        ver = next((v for v in svc["versions"] if v.get("version") == SERVICE_VERSION), None)
        if not ver:
            raise RuntimeError(f"{SERVICE}@{SERVICE_VERSION} not in catalog")
        if ver.get("supported") is False:
            raise RuntimeError(f"catalog says {SERVICE} doesn't support {ptag}")
        return f"{len(cat['services'])} services from {cat.get('registry_url')}"

    meta_id = f"{SERVICE}@{SERVICE_VERSION}"

    def local_state() -> str:
        cat = api.call("GET", "/v1/registry/catalog", timeout=60)
        return cat.get("local_state", {}).get(meta_id, "absent")

    def s_load() -> str:
        res = api.call("POST", "/v1/registry/load", {"name": SERVICE, "version": SERVICE_VERSION}, timeout=120)
        return f"state {res.get('state')}"

    def s_install() -> str:
        api.call("POST", "/v1/registry/install", {"name": SERVICE, "version": SERVICE_VERSION}, timeout=60)
        final = wait_for(f"{meta_id} installed",
                         lambda: (lambda s: s if s in ("installed", "failed") else None)(local_state()),
                         timeout=900, every=5)
        if final != "installed":
            raise RuntimeError(f"install ended {final} — see {log_path}")
        return "venv built"

    def s_start() -> str:
        name = "smoke-echo"
        res = api.call("POST", "/v1/service-request",
                       {"action": "create_service", "service_meta_id": meta_id, "proxy_name": name}, timeout=300)
        if res.get("status") == "failed":
            raise RuntimeError(f"create_service failed: {res.get('result')}")
        res = api.call("POST", "/v1/service-request",
                       {"action": "start_service", "service_proxy_id": name}, timeout=120)
        if res.get("status") == "failed":
            raise RuntimeError(f"start_service failed: {res.get('result')}")
        status = wait_for(f"{name} running",
                          lambda: (lambda p: p.get("status") if p.get("status") in ("running", "error") else None)(
                              api.call("GET", f"/v1/service-proxy/{name}")),
                          timeout=180)
        if status != "running":
            raise RuntimeError(f"{name} is {status} — see {log_path}")
        return f"{name} running"

    def s_update() -> str:
        res = api.call("GET", "/v1/update/check", timeout=30)
        return f"latest={res.get('latest')} available={res.get('update_available')} ({res.get('reason')})"

    print(f"botgear {args.version} smoke on {ptag} — work dir {work}", flush=True)
    try:
        for name, fn in [("download + verify", s_download), ("unpack", s_unpack), ("boot", s_boot),
                         ("claim first admin", s_claim), ("catalog", s_catalog),
                         (f"load {SERVICE}", s_load), (f"install {SERVICE}", s_install),
                         ("create + start instance", s_start), ("update check", s_update)]:
            if not step(name, fn):
                break
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
        passed = all(r["ok"] for r in results) and len(results) == 9
        summary = {
            "version": args.version, "platform": ptag, "passed": passed,
            "host": f"{platform.system()} {platform.release()} {platform.machine()}",
            "python": platform.python_version(), "steps": results,
            "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        print("\n" + json.dumps(summary, indent=2))
        if not passed and log_path.exists():
            print(f"\nlast lines of {log_path}:")
            print("\n".join(log_path.read_text(errors="replace").splitlines()[-25:]))
        if args.keep or not passed:
            print(f"\nkept: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
