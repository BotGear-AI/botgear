# unmanaged
"""Tests for subprocess→backend bus TLS wiring.

A subprocess service reaches the backend bus over a WebSocket. When the
backend serves TLS (``settings.ssl_enabled``), the subprocess MUST speak
``wss://`` — a plain ``ws://`` client reads the TLS handshake as a bad HTTP
response ("line without CRLF") and loops forever in reconnect, so it never
subscribes to its control topic and silently stops receiving actions
(set_panel, recenter, …). This regressed every subprocess service the
moment self-signed HTTPS was enabled on the backend.

These lock in the fix:
  * backend_url() scheme tracks settings.ssl_enabled
  * _to_ws_url maps https→wss / http→ws
  * BusClient builds a (non-verifying) SSL context for wss:// only
"""
from __future__ import annotations

import ssl
import types

import pytest

from botgear.runtime import subprocess_auth
from rlx_bus.client import BusClient, _to_ws_url


def _settings(port, ssl_enabled):
    return types.SimpleNamespace(port=port, ssl_enabled=ssl_enabled)


@pytest.mark.parametrize("ssl_enabled,scheme", [(True, "https"), (False, "http")])
def test_backend_url_scheme_tracks_ssl_enabled(monkeypatch, ssl_enabled, scheme):
    import config

    monkeypatch.setattr(config, "get_settings", lambda: _settings(8998, ssl_enabled))
    assert subprocess_auth.backend_url() == f"{scheme}://localhost:8998"


def test_backend_url_falls_back_to_http_when_settings_unavailable(monkeypatch):
    import config

    def _boom():
        raise RuntimeError("settings cache not set")

    monkeypatch.setattr(config, "get_settings", _boom)
    assert subprocess_auth.backend_url() == "http://localhost:8001"


def test_to_ws_url_maps_scheme():
    assert _to_ws_url("https://localhost:8998", "tok") == "wss://localhost:8998/v1/ws?token=tok"
    assert _to_ws_url("http://localhost:8998", "tok") == "ws://localhost:8998/v1/ws?token=tok"


def test_busclient_ssl_context_only_for_wss():
    wss = BusClient("https://localhost:8998", "tok")
    ctx = wss._ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    # Self-signed localhost cert: encrypt but don't verify.
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False

    plain = BusClient("http://localhost:8998", "tok")
    # Passing an SSL context to a ws:// URL raises in websockets, so None.
    assert plain._ssl_context() is None
