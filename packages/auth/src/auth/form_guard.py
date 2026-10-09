"""Guard for public (unauthenticated) forms: contact, inquiry, booking, ...

Three independent checks, each cheap and dependency-free:

1. per-client-IP rate limit (in-process sliding window);
2. bot checks: a honeypot input humans never see must stay empty, and the
   form must have been on screen for a minimum time (the page sends the
   milliseconds between render and submit as ``elapsed_ms``);
3. field validation is left to the caller — each form has its own fields.

Callers give a rate-limited client a real 429, but give a likely bot a
*fake* success (nothing stored, nothing sent) so it doesn't adapt.

    guard = FormGuard()
    ip = guard.client_ip(request)
    if guard.rate_limited(ip):
        return error_message("Too many submissions ...", 429)
    bot = guard.bot_reason(honeypot=data.pop("website", None),
                           elapsed_ms=data.pop("elapsed_ms", None))
    if bot:
        logger.warning("dropped likely bot from %s (%s)", ip, bot)
        return data   # fake success
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_email(value: Any) -> bool:
    return bool(EMAIL_RE.match(str(value or "").strip()))


def client_ip(request: Any) -> str:
    """The submitting client's IP. Behind nginx-proxy-manager, X-Real-IP is
    set by the proxy itself (a client can't spoof it); X-Forwarded-For and the
    socket peer are fallbacks for other setups."""
    if request is None:
        return "unknown"
    headers = getattr(request, "headers", {}) or {}
    real = headers.get("x-real-ip")
    if real:
        return real.strip()
    fwd = headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


class FormGuard:
    """One instance per form (each keeps its own rate-limit window)."""

    MAX_TRACKED_IPS = 10000

    def __init__(self, max_per_window: int = 10, window_s: float = 3600, min_elapsed_ms: int = 3000):
        self.max_per_window = max_per_window
        self.window_s = window_s
        self.min_elapsed_ms = min_elapsed_ms
        self._attempts: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    client_ip = staticmethod(client_ip)

    def rate_limited(self, ip: str, now: Optional[float] = None) -> bool:
        """Record one attempt from ``ip``; True when it exceeds the window."""
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._attempts.setdefault(ip, deque())
            while q and now - q[0] > self.window_s:
                q.popleft()
            q.append(now)
            if len(self._attempts) > self.MAX_TRACKED_IPS:
                for key in [k for k, v in self._attempts.items() if not v or now - v[-1] > self.window_s]:
                    self._attempts.pop(key, None)
            return len(q) > self.max_per_window

    def bot_reason(self, honeypot: Any, elapsed_ms: Any) -> Optional[str]:
        """Why this looks like a bot, or None for a plausible human."""
        if str(honeypot or "").strip():
            return "honeypot filled"
        if elapsed_ms is None:
            return "no timing value (not sent by the site's form)"
        try:
            if int(elapsed_ms) < self.min_elapsed_ms:
                return f"submitted {int(elapsed_ms)} ms after the form was shown"
        except (TypeError, ValueError):
            return "invalid timing value"
        return None
