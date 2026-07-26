"""oscope_service entry point — delegates to SubprocessService.run()."""
from __future__ import annotations

import sys

from .service import OscopeService


def main() -> int:
    return OscopeService.run()


if __name__ == "__main__":
    sys.exit(main())
