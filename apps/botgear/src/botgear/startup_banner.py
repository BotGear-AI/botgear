# unmanaged
"""Console banner printed once the server is listening: logo, version, and
plain next steps — including the setup link when no admin account exists
yet. ASCII only, so an old Windows console code page can't choke on it.
"""
from __future__ import annotations

import logging
import os
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


# Console browsers webbrowser can fall back to on a display-less box; opening
# one would take over the terminal BotGear is printing to.
_TEXT_BROWSERS = {"lynx", "w3m", "links", "elinks", "www-browser", "browsh"}


def headless_reason(env: Optional[dict] = None, platform: Optional[str] = None,
                    interactive: Optional[bool] = None) -> Optional[str]:
    """Why this process can't usefully open a browser, or None if it can.

    Headless = nobody is sitting at a screen attached to this process: a
    service (systemd, launchd, Windows service), a container, CI, an SSH
    session, or — on Linux/BSD — no X11/Wayland display at all.
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if interactive is None:
        try:
            interactive = sys.stdin.isatty() and sys.stdout.isatty()
        except (AttributeError, ValueError):
            interactive = False
    if env.get("BOTGEAR_RESTARTED"):
        return "restart"
    if env.get("CI"):
        return "CI"
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return "SSH session"
    if env.get("container") or os.path.exists("/.dockerenv"):
        return "container"
    if not interactive:
        return "not started from a terminal"
    if platform.startswith(("linux", "freebsd", "openbsd", "netbsd")) and not (
            env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return "no graphical display"
    return None


def open_browser(url: str) -> str:
    """Open ``url`` in the default GUI browser. Returns a short status line."""
    reason = headless_reason()
    if reason:
        return f"Not opening a browser ({reason}) -- open the link above yourself."
    try:
        import webbrowser
        browser = webbrowser.get()
        name = (getattr(browser, "name", "") or type(browser).__name__).lower()
        if any(t in name for t in _TEXT_BROWSERS):
            return "No graphical browser found -- open the link above yourself."
        if browser.open(url, new=2):
            return "Opening it in your browser..."
    except Exception:  # noqa: BLE001 — webbrowser.Error, no browser registered, ...
        pass
    return "Couldn't open a browser -- open the link above yourself."


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
    'running on' line instead of scrolling above it; then, when
    ``open_browser`` is on and someone is at the screen, open the browser."""
    def _print() -> None:
        try:
            port = int(getattr(settings, "port", None) or 8998)
            ssl = bool(getattr(settings, "ssl_enabled", False))
            text = render(
                version=version,
                port=port,
                ssl=ssl,
                data_dir=getattr(settings, "data_dir", None),
                has_users=_has_users(),
            )
            if getattr(settings, "open_browser", False):
                status = open_browser(f"{'https' if ssl else 'http'}://localhost:{port}")
                text += f"  {status}\n"
            sys.stdout.write("\n" + text + "\n")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            logger.debug("startup banner failed", exc_info=True)

    t = threading.Timer(delay_s, _print)
    t.daemon = True
    t.start()
