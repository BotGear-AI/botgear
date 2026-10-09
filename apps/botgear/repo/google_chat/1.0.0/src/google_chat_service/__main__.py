"""google_chat_service entry point.

Boilerplate (env, bus client, hello, control subscription, heartbeat,
signals, graceful shutdown) lives in ``rlx_bus.SubprocessService.run``.
"""
from __future__ import annotations

import sys

from .service import GoogleChatService


def main() -> int:
    return GoogleChatService.run()


if __name__ == "__main__":
    sys.exit(main())
