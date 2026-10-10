"""No customer-specific names in botgear's own source (apps/botgear,
apps/botgear_ui) — both are published to the public BotGear-AI/botgear repo.
Same sha256-hashed name list as packages/testkit/tests/test_customer_neutral.py,
so this file doesn't itself list them.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

FORBIDDEN = {
    "0885ca53a92ca61d",
    "194aee0a144b2adb",
    "1d5c5528a13c8d1f",
    "2690b3258a7fe6e6",
    "38db60b0b8c31f28",
    "4c6a65a11440c7d9",
    "69508e23ad26ce4e",
    "6e649ccc4f5484fa",
    "80c0ee5c965bcecb",
    "88f82baf2a0bbf4a",
    "8be76a4b26925ad0",
    "9427961140d892e2",
    "971f8bcc3ce77851",
    "b7464e20cd9bec12",
}

APPS = Path(__file__).resolve().parents[2]          # .../apps
ROOTS = ("botgear", "botgear_ui")
SKIP_PARTS = {"node_modules", "dist", ".venv", "build", "data", "logs", "__pycache__"}
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".md", ".yml", ".yaml", ".toml", ".json", ".sh", ".html", ".css", ".txt", ".example"}


def _files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z", *ROOTS], cwd=APPS, capture_output=True,
                             text=True, check=True).stdout
        paths = [APPS / p for p in out.split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        paths = [p for r in ROOTS for p in (APPS / r).rglob("*")]
    return [p for p in paths if p.is_file() and not SKIP_PARTS.intersection(p.parts)
            and p.suffix.lower() in TEXT_SUFFIXES and p.name != "package-lock.json"]


def _hits(text: str) -> set[str]:
    text = text.lower()
    words = set(re.findall(r"[a-z0-9]+", text)) | set(re.findall(r"[a-z0-9_]+", text))
    return {w for w in words if hashlib.sha256(w.encode()).hexdigest()[:16] in FORBIDDEN}


def test_no_customer_names_in_botgear_source():
    offenders = []
    for path in _files():
        try:
            hits = _hits(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, OSError):
            continue
        if hits:
            offenders.append(f"{path.relative_to(APPS)}: {sorted(hits)}")
    assert not offenders, "customer-specific names in botgear source:\n  " + "\n  ".join(offenders)
