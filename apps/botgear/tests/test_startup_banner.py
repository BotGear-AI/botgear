"""Startup banner text + the per-install JWT secret for packaged builds."""
import os
import stat
import sys

import pytest

from botgear import event_handlers, startup_banner
from auth.jwt_util import FALLBACK_JWT_SECRET


def test_banner_first_run_gives_the_setup_link():
    text = startup_banner.render("0.2.0", 8998, True, None, has_users=False)
    assert "BotGear 0.2.0 is running." in text
    assert "Open  https://localhost:8998" in text
    assert "create the admin account" in text
    assert "continue to localhost" in text          # self-signed cert step
    text.encode("ascii")                              # old Windows consoles


def test_banner_after_setup_just_says_sign_in():
    text = startup_banner.render("0.2.0", 8998, True, None, has_users=True)
    assert "create the admin account" not in text
    assert "https://localhost:8998  and sign in" in text


def test_banner_plain_http_and_dev_build():
    text = startup_banner.render("0.0.0", 8001, False, None, has_users=False)
    assert "BotGear (dev build) is running." in text
    assert "http://localhost:8001" in text
    assert "isn't private" not in text


@pytest.fixture
def no_secret(monkeypatch):
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    monkeypatch.delenv("BOTGEAR_JWT_SECRET", raising=False)
    yield


def test_frozen_build_generates_and_reuses_a_private_secret(no_secret, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    event_handlers._ensure_jwt_secret(str(tmp_path))
    secret = os.environ["JWT_SECRET_KEY"]
    path = tmp_path / "security" / "jwt_secret"
    assert len(secret) == 64 and secret != FALLBACK_JWT_SECRET
    assert path.read_text().strip() == secret
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    monkeypatch.delenv("JWT_SECRET_KEY")
    event_handlers._ensure_jwt_secret(str(tmp_path))
    assert os.environ["JWT_SECRET_KEY"] == secret     # same secret next start


def test_explicit_secret_wins(no_secret, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 64)
    event_handlers._ensure_jwt_secret(str(tmp_path))
    assert os.environ["JWT_SECRET_KEY"] == "x" * 64
    assert not (tmp_path / "security" / "jwt_secret").exists()


def test_source_runs_keep_the_shared_default(no_secret, monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    event_handlers._ensure_jwt_secret(str(tmp_path))
    assert "JWT_SECRET_KEY" not in os.environ


def test_ssl_cert_lives_under_the_working_dir(monkeypatch, tmp_path):
    # var/ssl in a packaged build (cwd = var), apps/botgear/ssl from source —
    # never the shared temp-dir default.
    monkeypatch.chdir(tmp_path)
    cert, key = event_handlers.get_ssl_paths(None)
    assert cert == tmp_path / "ssl" / "cert.pem"
    assert key == tmp_path / "ssl" / "key.pem"
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / "ssl").stat().st_mode) == 0o700


# ─── open_browser: only when someone is at a screen ─────────────────────

DESKTOP = {"DISPLAY": ":0"}


@pytest.mark.parametrize("env, plat, tty, expect", [
    (DESKTOP, "linux", True, None),
    ({"WAYLAND_DISPLAY": "wayland-0"}, "linux", True, None),
    ({}, "darwin", True, None),
    ({}, "win32", True, None),
    ({}, "linux", True, "no graphical display"),          # headless Pi / server
    (DESKTOP, "linux", False, "not started from a terminal"),  # systemd, nohup
    ({**DESKTOP, "SSH_CONNECTION": "1 2 3 4"}, "linux", True, "SSH session"),
    ({"SSH_TTY": "/dev/pts/1"}, "darwin", True, "SSH session"),
    ({**DESKTOP, "CI": "true"}, "linux", True, "CI"),
    ({**DESKTOP, "container": "podman"}, "linux", True, "container"),
    ({**DESKTOP, "BOTGEAR_RESTARTED": "1"}, "linux", True, "restart"),
])
def test_headless_reason(env, plat, tty, expect, monkeypatch):
    monkeypatch.setattr(startup_banner.os.path, "exists", lambda p: False, raising=False)
    assert startup_banner.headless_reason(env=env, platform=plat, interactive=tty) == expect


def test_open_browser_skips_headless(monkeypatch):
    opened = []
    monkeypatch.setattr(startup_banner, "headless_reason", lambda: "no graphical display")
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: opened.append(a) or True)
    assert "no graphical display" in startup_banner.open_browser("https://localhost:8998")
    assert opened == []


def test_open_browser_refuses_text_browsers(monkeypatch):
    import webbrowser
    monkeypatch.setattr(startup_banner, "headless_reason", lambda: None)
    lynx = webbrowser.GenericBrowser("lynx")
    monkeypatch.setattr(webbrowser, "get", lambda *a: lynx)
    monkeypatch.setattr(lynx, "open", lambda *a, **k: pytest.fail("must not launch lynx"))
    assert "No graphical browser" in startup_banner.open_browser("https://localhost:8998")


def test_open_browser_opens_on_a_desktop(monkeypatch):
    import webbrowser
    calls = []

    class Fake(webbrowser.BaseBrowser):
        def open(self, url, new=0, autoraise=True):
            calls.append((url, new))
            return True

    monkeypatch.setattr(startup_banner, "headless_reason", lambda: None)
    monkeypatch.setattr(webbrowser, "get", lambda *a: Fake("xdg-open"))
    assert startup_banner.open_browser("https://localhost:8998") == "Opening it in your browser..."
    assert calls == [("https://localhost:8998", 2)]
