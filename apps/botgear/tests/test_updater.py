"""Unit tests for the self-update version/asset logic (pure helpers)."""
from __future__ import annotations

import pytest

from botgear.runtime import updater
from botgear.runtime.updater import ReleaseInfo


@pytest.mark.parametrize("s,expected", [
    ("0.1.42", (0, 1, 42)),
    ("v0.1.0", (0, 1, 0)),      # legacy 'v' prefix
    (" 1.2.3 ", (1, 2, 3)),     # surrounding whitespace
    ("10.20.30", (10, 20, 30)),
    ("0.0.0", None),            # dev/unknown fallback sentinel
    ("latest", None),
    ("0.1", None),             # not three components
    ("0.1.0-deadbeef", None),  # dev '-<sha>' suffix
    ("", None),
    (None, None),
    (42, None),                # non-str
])
def test_parse_version(s, expected):
    assert updater.parse_version(s) == expected


def test_is_release_version():
    assert updater.is_release_version("0.1.42")
    assert not updater.is_release_version("0.0.0")
    assert not updater.is_release_version("latest")


@pytest.mark.parametrize("cur,latest,available", [
    ("0.1.0", "0.1.1", True),       # newer build
    ("0.1.0", "v0.1.0", False),     # equal (legacy tag)
    ("0.1.5", "0.1.2", False),      # running newer than latest
    ("0.1.0", "0.2.0", True),       # newer minor
    ("0.0.0", "0.1.0", False),      # dev build → never offered
    ("0.1.0", "latest", False),     # unparseable latest
    ("0.1.0-abc", "0.1.9", False),  # dev current → never offered
])
def test_decide(cur, latest, available):
    got, reason = updater.decide(cur, latest)
    assert got is available, reason
    assert isinstance(reason, str) and reason


def test_platform_tag_shape():
    tag = updater.platform_tag()
    assert "-" in tag
    os_part, arch_part = tag.split("-", 1)
    assert os_part in {"linux", "darwin", "windows"}


@pytest.mark.parametrize("system, machine, tag", [
    # Must equal packaging/build.sh's PLATFORM_TAG for the same host.
    ("Linux", "x86_64", "linux-x86_64"),
    ("Linux", "aarch64", "linux-aarch64"),
    ("Darwin", "arm64", "darwin-arm64"),
    ("Windows", "AMD64", "windows-x86_64"),
])
def test_platform_tag_matches_build_sh(monkeypatch, system, machine, tag):
    monkeypatch.setattr(updater.platform, "system", lambda: system)
    monkeypatch.setattr(updater.platform, "machine", lambda: machine)
    assert updater.platform_tag() == tag


def test_self_update_is_posix_only():
    assert updater.self_update_supported("linux-x86_64")
    assert updater.self_update_supported("darwin-arm64")
    assert not updater.self_update_supported("windows-x86_64")


def test_platform_asset_matches_by_suffix():
    rel = ReleaseInfo(
        version="0.1.7",
        assets={
            "botgear-0.1.7-linux-aarch64.tar.gz": "https://x/aarch64.tar.gz",
            "botgear-0.1.7-linux-aarch64.tar.gz.sha256": "https://x/aarch64.sha256",
            "botgear-0.1.7-linux-x86_64.tar.gz": "https://x/x86.tar.gz",
            "botgear-0.1.7-linux-x86_64.tar.gz.sha256": "https://x/x86.sha256",
        },
    )
    tar, sha = rel.platform_asset("linux-aarch64")
    assert tar == "https://x/aarch64.tar.gz"
    assert sha == "https://x/aarch64.sha256"
    tar, sha = rel.platform_asset("linux-x86_64")
    assert tar == "https://x/x86.tar.gz"
    # missing platform → (None, None), not a crash
    assert rel.platform_asset("darwin-arm64") == (None, None)
