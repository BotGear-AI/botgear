"""The library must stay generic: no application or monorepo-package
imports, so it can move to packages/agent_harness unchanged."""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "agent_harness"
ALLOWED_THIRD_PARTY = {"yaml", "openai", "anthropic"}
# Application, product and customer names the library must never mention,
# stored as sha256 prefixes so this public file doesn't itself list them.
# Add one: hashlib.sha256(word.encode()).hexdigest()[:16] (lowercase word).
FORBIDDEN_WORD_HASHES = {
    "f7496f1fc9731f2e",
    "cb0356a0532e824b",
    "c05b49dcd139b904",
    "88f82baf2a0bbf4a",
    "b7464e20cd9bec12",
    "6e649ccc4f5484fa",
    "1d5c5528a13c8d1f",
    "80c0ee5c965bcecb",
    "971f8bcc3ce77851",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_only_stdlib_and_allowed_third_party():
    import sys
    stdlib = set(sys.stdlib_module_names)
    for path in SRC.rglob("*.py"):
        bad = {n for n in _imports(path) if n not in stdlib and n not in ALLOWED_THIRD_PARTY and n != "agent_harness"}
        assert not bad, f"{path.relative_to(SRC)} imports {sorted(bad)}"


def test_no_domain_or_customer_names():
    import hashlib
    import re
    for path in SRC.rglob("*.py"):
        words = set(re.findall(r"[a-z0-9]+", path.read_text(encoding="utf-8").lower()))
        for word in words:
            digest = hashlib.sha256(word.encode()).hexdigest()[:16]
            assert digest not in FORBIDDEN_WORD_HASHES, f"{path.relative_to(SRC)} mentions {word!r}"
