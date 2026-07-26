# unmanaged
"""Framework self-update — version discovery + comparison.

The runtime asks its update repo's GitHub Releases (``config.update_repo``)
whether a newer framework build exists, compares the latest release tag
against the running version, and resolves the download asset for THIS
platform.

Design: everything here except :func:`fetch_latest_release` is a PURE
function (no I/O) so the comparison/asset logic is unit-tested without a
network. ``fetch_latest_release`` is the one side-effecting call and is
kept thin + defensive — the route turns any failure into a clean
"couldn't check" rather than a 500.

Versioning contract (docs/TODO_UPGRADE_FEATURE.md §7/§8): the build version
is ``MAJOR.MINOR.BUILD`` (e.g. ``0.1.42``) where BUILD is an opaque,
monotonically-increasing build id (the CI run number). We compare as an
integer tuple. Sentinels — ``latest``, ``0.0.0``, or a ``-<sha>`` dev
suffix — are NOT real releases, so we never offer to update FROM them.
"""
from __future__ import annotations

import logging
import platform
import re
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# A clean release tag: MAJOR.MINOR.BUILD, with an optional legacy ``v``
# prefix (pre-0.1.x releases were tagged ``v0.1.0``). Anything else —
# ``latest``, ``0.1.0-<sha>``, empty — is not a comparable release.
_RELEASE_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(s: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """``"0.1.42"`` / legacy ``"v0.1.0"`` → ``(0, 1, 42)``; non-releases → ``None``."""
    if not isinstance(s, str):
        return None
    m = _RELEASE_RE.match(s.strip())
    if not m:
        return None
    t = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    # ``0.0.0`` is the dev/unknown fallback stamp (no version.json / VERSION
    # unset), never a published release — treat it as non-comparable so a dev
    # build is never told it's behind.
    return None if t == (0, 0, 0) else t


def is_release_version(s: Optional[str]) -> bool:
    """True iff ``s`` is a clean ``MAJOR.MINOR.BUILD`` (not a dev/sentinel build)."""
    return parse_version(s) is not None


def platform_tag() -> str:
    """The bundle platform tag, matching packaging/build.sh's PLATFORM_TAG
    (e.g. ``linux-x86_64``, ``linux-aarch64``)."""
    osname = {
        "linux": "linux", "darwin": "macos", "windows": "windows",
    }.get(platform.system().lower(), platform.system().lower())
    arch = {
        "x86_64": "x86_64", "amd64": "x86_64",
        "aarch64": "aarch64", "arm64": "aarch64",
    }.get(platform.machine().lower(), platform.machine().lower())
    return f"{osname}-{arch}"


def decide(current: Optional[str], latest: Optional[str]) -> Tuple[bool, str]:
    """``(update_available, human_reason)`` from two version strings.

    Conservative + fail-safe: only a clean release strictly greater than a
    clean current release counts as available. A dev/sentinel running build
    is never offered an update (it would see itself as infinitely behind).
    """
    cur, lat = parse_version(current), parse_version(latest)
    if cur is None:
        return (False, f"this is a dev/non-release build ({current!r}); updates are disabled")
    if lat is None:
        return (False, f"latest release tag {latest!r} is not a recognizable version")
    if lat > cur:
        return (True, f"{_fmt(lat)} is newer than {_fmt(cur)}")
    if lat == cur:
        return (False, "up to date")
    return (False, f"running a newer build than the latest release ({_fmt(cur)} > {_fmt(lat)})")


def _fmt(t: Tuple[int, int, int]) -> str:
    return ".".join(str(x) for x in t)


@dataclass
class ReleaseInfo:
    """A GitHub Release, reduced to what the updater needs."""
    version: str                                   # the tag, verbatim (may carry a legacy 'v')
    name: Optional[str] = None
    published_at: Optional[str] = None
    notes: Optional[str] = None
    assets: Dict[str, str] = field(default_factory=dict)  # filename -> download URL

    def platform_asset(self, ptag: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """``(tarball_url, sha256_url)`` for ``ptag`` (default = this host).

        Matches by the ``-<platform>.tar.gz`` SUFFIX rather than reconstructing
        the full filename, so it's robust to version-string formatting (bare
        vs legacy ``v``)."""
        suffix = f"-{ptag or platform_tag()}.tar.gz"
        tarball = next((u for n, u in self.assets.items() if n.endswith(suffix)), None)
        sha = next((u for n, u in self.assets.items() if n.endswith(suffix + ".sha256")), None)
        return tarball, sha


# GitHub requires a User-Agent on API requests (else 403). We use the
# stdlib urllib (NOT httpx) so this works inside the frozen PyInstaller
# bundle, where lazily-imported third-party deps aren't collected — and so
# there's one less thing that can be missing from a build.
_UA = "botcore-updater"


def _fetch_latest_release_sync(repo: str, timeout: float) -> ReleaseInfo:
    import json
    import urllib.request

    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": _UA,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https URL
        data = json.loads(resp.read().decode("utf-8"))

    assets = {
        a["name"]: a.get("browser_download_url")
        for a in (data.get("assets") or [])
        if isinstance(a, dict) and a.get("name") and a.get("browser_download_url")
    }
    return ReleaseInfo(
        version=str(data.get("tag_name") or "").strip(),
        name=data.get("name") or None,
        published_at=data.get("published_at") or None,
        notes=data.get("body") or None,
        assets=assets,
    )


async def fetch_latest_release(repo: str, *, timeout: float = 10.0) -> ReleaseInfo:
    """Fetch the latest GitHub Release for ``owner/name`` (stdlib urllib, off
    the event loop). Raises on network/HTTP error — the caller turns that into
    a graceful "couldn't check" response."""
    import asyncio
    return await asyncio.to_thread(_fetch_latest_release_sync, repo, timeout)
