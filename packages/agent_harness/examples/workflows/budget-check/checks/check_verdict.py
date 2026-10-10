"""Check for budget-check: recompute the totals independently, then confirm both
the numbers in totals.json and the verdict on verdict.md's first line.
Exit 0 = pass."""
import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--expenses", type=Path, required=True)
parser.add_argument("--budget", type=float, required=True)
parser.add_argument("--totals", type=Path, required=True)
parser.add_argument("--verdict", type=Path, required=True)
args = parser.parse_args()

sums: dict[str, float] = defaultdict(float)
with args.expenses.open(newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        sums[row["category"]] += float(row["amount"])
total = round(sum(sums.values()), 2)
expected_verdict = "OVER BUDGET" if total > args.budget else "WITHIN BUDGET"

problems = []
try:
    got = json.loads(args.totals.read_text(encoding="utf-8"))
    if abs(float(got.get("total", -1)) - total) > 0.005:
        problems.append(f"totals.json total {got.get('total')} != actual {total}")
except (OSError, ValueError, AttributeError) as exc:
    problems.append(f"totals.json unreadable: {exc}")
try:
    lines = args.verdict.read_text(encoding="utf-8").strip().splitlines()
    first = lines[0].strip().strip("#* ").upper() if lines else ""
    if first != expected_verdict:
        problems.append(f"verdict.md says {first!r}, but {total} vs budget {args.budget} is {expected_verdict!r}")
except OSError as exc:
    problems.append(f"verdict.md unreadable: {exc}")

for p in problems:
    print(f"FAIL: {p}")
if problems:
    sys.exit(1)
print(f"PASS: total {total} vs budget {args.budget} -> {expected_verdict}")
