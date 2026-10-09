from types import SimpleNamespace

from auth.form_guard import FormGuard, client_ip, valid_email


def req(headers=None, host="10.0.0.9"):
    return SimpleNamespace(headers=headers or {}, client=SimpleNamespace(host=host))


def test_client_ip_prefers_proxy_set_real_ip():
    assert client_ip(req({"x-real-ip": " 1.2.3.4 ", "x-forwarded-for": "5.6.7.8"})) == "1.2.3.4"
    assert client_ip(req({"x-forwarded-for": "5.6.7.8, 10.0.0.1"})) == "5.6.7.8"
    assert client_ip(req()) == "10.0.0.9"
    assert client_ip(None) == "unknown"


def test_rate_limit_is_a_sliding_window_per_ip():
    g = FormGuard(max_per_window=2, window_s=60)
    assert [g.rate_limited("a", now=t) for t in (0, 1, 2)] == [False, False, True]
    assert g.rate_limited("b", now=2) is False          # other IPs unaffected
    assert g.rate_limited("a", now=100) is False        # window has passed


def test_instances_keep_separate_windows():
    a, b = FormGuard(max_per_window=1), FormGuard(max_per_window=1)
    a.rate_limited("x", now=0)
    assert b.rate_limited("x", now=0) is False


def test_bot_reason():
    g = FormGuard(min_elapsed_ms=3000)
    assert g.bot_reason("", 5000) is None
    assert g.bot_reason(None, "5000") is None
    assert g.bot_reason("http://spam", 5000) == "honeypot filled"
    assert "no timing" in g.bot_reason("", None)
    assert "1200 ms" in g.bot_reason("", 1200)
    assert g.bot_reason("", "abc") == "invalid timing value"


def test_valid_email():
    assert valid_email(" a@b.co ")
    assert not valid_email("a@b") and not valid_email("") and not valid_email(None)
