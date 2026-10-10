"""Print {file name: word count} for every .txt file in a folder, as JSON."""
import json
import sys
from pathlib import Path


def count_words(folder: Path) -> dict[str, int]:
    return {p.name: len(p.read_text(encoding="utf-8").split()) for p in sorted(Path(folder).glob("*.txt"))}


if __name__ == "__main__":
    print(json.dumps(count_words(Path(sys.argv[1])), indent=2))
