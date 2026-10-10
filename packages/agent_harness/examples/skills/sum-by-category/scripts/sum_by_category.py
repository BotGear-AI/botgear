"""Print per-category and overall totals of an expenses CSV (date,category,amount) as JSON."""
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path


def sum_by_category(path: Path) -> dict:
    totals: dict[str, float] = defaultdict(float)
    with Path(path).open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            totals[row["category"]] += float(row["amount"])
    by_category = {k: round(v, 2) for k, v in sorted(totals.items())}
    return {"by_category": by_category, "total": round(sum(by_category.values()), 2)}


if __name__ == "__main__":
    print(json.dumps(sum_by_category(Path(sys.argv[1])), indent=2))
