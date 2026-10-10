"""Check for word-report: recount independently; counts.json must agree. Exit 0 = pass."""
import argparse
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--docs", type=Path, required=True)
parser.add_argument("--counts", type=Path, required=True)
args = parser.parse_args()

expected = {p.name: len(p.read_text(encoding="utf-8").split()) for p in sorted(args.docs.glob("*.txt"))}
try:
    got = json.loads(args.counts.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    print(f"FAIL: counts.json unreadable: {exc}")
    sys.exit(1)
if got != expected:
    print(f"FAIL: counts.json {got} != actual {expected}")
    sys.exit(1)
print(f"PASS: {len(expected)} document(s) counted correctly")
