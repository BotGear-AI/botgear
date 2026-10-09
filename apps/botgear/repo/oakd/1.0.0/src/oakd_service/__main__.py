"""oakd_service entry point.

All boilerplate (env loading, bus client, hello announce, control topic
subscription, signal handling, graceful shutdown) lives in
``rlx_bus.SubprocessService.run``.
"""
from __future__ import annotations

import sys

from .service import OakdService


def main() -> int:
    return OakdService.run()


if __name__ == "__main__":
    sys.exit(main())
