#!/usr/bin/env python
"""One-shot migration: write ``registry`` into every config-set's runtime.yml.

The registry = the current sorted .yml file order (byte-identical to the old
`sorted(iterdir)` membership), so each set boots the SAME services in the same
order after this runs. Idempotent. Run once from apps/botcore:

    .venv/bin/python scripts/migrate_registry.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from botcore.runtime.boot import _registry_members
from botcore.runtime.config_sets import write_runtime_registry


def main() -> int:
    root = Path("data/config_sets")
    if not root.is_dir():
        print(f"no {root} (run from apps/botcore)", file=sys.stderr)
        return 1
    for set_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        members = _registry_members(set_dir)
        # equivalence guard: registry must equal today's glob membership exactly
        glob = sorted(
            p.stem for p in set_dir.iterdir()
            if p.is_file() and p.suffix == ".yml"
            and p.name != "runtime.yml" and not p.name.startswith(".")
        )
        assert members == glob, (set_dir.name, members, glob)
        write_runtime_registry(set_dir, members)
        print(f"{set_dir.name:10} → {len(members)} members  (first: {members[:3]}…)")
    print("OK — registry written to all sets (== sorted glob membership)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
