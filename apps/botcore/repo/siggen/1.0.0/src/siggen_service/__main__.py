"""siggen_service entry point — delegates to SubprocessService.run()."""
from __future__ import annotations

import sys

from .service import SiggenService


def main() -> int:
    return SiggenService.run()


if __name__ == "__main__":
    sys.exit(main())
