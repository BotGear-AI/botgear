# unmanaged
"""Console banner printed once the server is listening: logo, version, and
plain next steps — including the setup link when no admin account exists
yet. ASCII only, so an old Windows console code page can't choke on it.
"""
from __future__ import annotations

import logging
import socket
import sys
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Generated from botgear_ui/public/botgear-logo.png (PIL, 40x20 cells) and
# pyfiglet's "standard" font. Regenerate rather than hand-edit.
LOGO = r"""
                  ####
            ###++######++###
           +#####++  ++#####+
            ###  ###### +###
         #####+ ######## +#####
         ###### #######+ ######
            ####  ++++ +####
           #################+
            +##++######++##+
                  ++++
      ++ +#++###+      +###+##+ ++
     +##+##+###############++##+##+
     ###+#+##################+#+###
    +###+ #################### +###+
    #####  ##################  #####
    +##+   +################+   +###
  ++#### +  ################  + #### +
 ###++++###   +##++ +++##+   ###++++###
+##+    ###    +########+    ###    ###+
 ##+    +##      ######      ##+    +##
"""

TITLE = r"""
 ____        _    ____
| __ )  ___ | |_ / ___| ___  __ _ _ __
|  _ \ / _ \| __| |  _ / _ \/ _` | '__|
| |_) | (_) | |_| |_| |  __/ (_| | |
|____/ \___/ \__|\____|\___|\__,_|_|
"""


def _lan_ip() -> Optional[str]:
    """This machine's address on the local network, best effort. A UDP
    connect sends nothing; it only asks the OS which interface it would use."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


def _has_users() -> Optional[bool]:
    try:
        from database.factory import get_database_client
        from auth.local_auth import USERS_TABLE
        return bool(get_database_client().get_all_items(USERS_TABLE))
    except Exception:  # noqa: BLE001 — the banner must never break startup
        return None


def render(version: str, port: int, ssl: bool, data_dir: Optional[str], has_users: Optional[bool]) -> str:
    scheme = "https" if ssl else "http"
    local = f"{scheme}://localhost:{port}"
    lan = _lan_ip()
    label = f"BotGear {version}" if version and version not in ("0.0.0", "latest") else "BotGear (dev build)"
    lines = [LOGO.rstrip("\n"), TITLE.rstrip("\n"), "", f"  {label} is running.", ""]
    if has_users is False:
        lines += [
            "  Finish setting up -- create the admin account:",
            "",
            f"    1. Open  {local}",
        ]
        if ssl:
            lines += [
                "    2. Your browser will warn that the connection isn't private.",
                "       BotGear made its own certificate on first start; that's expected.",
                "       Choose Advanced, then continue to localhost.",
                "    3. Enter an email and a password (8+ characters).",
                "       That account becomes the administrator.",
            ]
        else:
            lines += [
                "    2. Enter an email and a password (8+ characters).",
                "       That account becomes the administrator.",
            ]
    else:
        lines += [f"  Open  {local}  and sign in."]
    lines += [""]
    if lan:
        lines += [f"  From another device on your network:  {scheme}://{lan}:{port}"]
    if data_dir:
        lines += [f"  Your data is kept in:  {Path(data_dir).resolve()}"]
    lines += ["  Stop BotGear with Ctrl+C.", ""]
    return "\n".join(lines)


def schedule(settings, version: str, delay_s: float = 1.5) -> None:
    """Print the banner shortly after startup, so it lands below uvicorn's
    'running on' line instead of scrolling above it."""
    def _print() -> None:
        try:
            text = render(
                version=version,
                port=int(getattr(settings, "port", None) or 8998),
                ssl=bool(getattr(settings, "ssl_enabled", False)),
                data_dir=getattr(settings, "data_dir", None),
                has_users=_has_users(),
            )
            sys.stdout.write("\n" + text + "\n")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            logger.debug("startup banner failed", exc_info=True)

    t = threading.Timer(delay_s, _print)
    t.daemon = True
    t.start()
