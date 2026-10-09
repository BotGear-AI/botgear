import tempfile
import os
import pytest
from unittest.mock import patch, MagicMock
from auth.verify_email import (
    send_verification_email,
    send_reset_password_email,
    send_email,
    EmailSendError,
    _render_html_content,
)

class DummyConfig:
    smtp_user = "user"
    smtp_password = "pass"
    smtp_host = "smtp.example.com"
    smtp_port = 587
    smtp_from = "noreply@example.com"
    base_url = "https://example.com"
    verify_email_url = "verify/{registration.verification_token}"
    reset_password_url = "reset/{reset_token}"

def test_send_verification_email_sends_email():
    config = DummyConfig()
    registration = type('Registration', (), {
        'email': 'test@example.com',
        'verification_token': 'abc123',
        'created': 1234567890,
        'client_base_url': 'http://localhost:3000',
    })()
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = smtp_instance
        send_verification_email(config, registration)
        smtp_instance.starttls.assert_called_once()
        smtp_instance.login.assert_called_once_with(config.smtp_user, config.smtp_password)
        smtp_instance.send_message.assert_called_once()
        # Check that the verification link is in the email body
        sent_msg = smtp_instance.send_message.call_args[0][0]
        assert registration.verification_token in sent_msg.get_content()
        assert config.verify_email_url.split("{")[0] in sent_msg.get_content()

def test_send_reset_password_email_sends_email():
    config = DummyConfig()
    user_email = "reset@example.com"
    token = "reset456"
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = smtp_instance
        send_reset_password_email(config, user_email, token)
        smtp_instance.starttls.assert_called_once()
        smtp_instance.login.assert_called_once_with(config.smtp_user, config.smtp_password)
        smtp_instance.send_message.assert_called_once()
        sent_msg = smtp_instance.send_message.call_args[0][0]
        assert token in sent_msg.get_content()
        assert config.reset_password_url.split("{")[0] in sent_msg.get_content()


# #301 — delivery failures must NOT be swallowed: the send functions raise so the
# caller can surface/retry instead of reporting a false success.

def _reg():
    return type('Registration', (), {
        'email': 'test@example.com', 'verification_token': 'abc123',
        'created': 1234567890, 'client_base_url': 'http://localhost:3000',
    })()


def test_send_verification_email_raises_on_smtp_failure():
    with patch("smtplib.SMTP", side_effect=OSError("connection refused")):
        with pytest.raises(EmailSendError):
            send_verification_email(DummyConfig(), _reg())


def test_send_reset_password_email_raises_on_smtp_failure():
    with patch("smtplib.SMTP", side_effect=OSError("connection refused")):
        with pytest.raises(EmailSendError):
            send_reset_password_email(DummyConfig(), "reset@example.com", "tok")


# ── HTML template support (optional, additive parameter) ───────────────────

def test_render_html_content_substitutes_double_brace_tokens_only():
    # CSS braces (single, not doubled) must survive untouched -- str.format()
    # would raise on these; _render_html_content must not.
    template = "<style>body { margin: 0; }</style><a href='{{link}}'>{{link}}</a>"
    rendered = _render_html_content(template, link="https://example.com/reset")
    assert "body { margin: 0; }" in rendered
    assert "https://example.com/reset" in rendered
    assert "{{link}}" not in rendered


def test_render_html_content_raises_on_unknown_token():
    with pytest.raises(KeyError):
        _render_html_content("<p>{{missing}}</p>")


def test_render_html_content_escapes_substituted_values():
    # A value containing markup/quotes (e.g. a caller-supplied client_base_url
    # reflected into verification_link) must not break out of the href
    # attribute or inject a tag into the rendered email.
    template = "<a href='{{link}}'>{{link}}</a>"
    rendered = _render_html_content(
        template, link="'><script>alert(1)</script>"
    )
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "&#x27;&gt;" in rendered or "&#39;&gt;" in rendered


def test_reset_password_email_with_html_template_sends_multipart():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
        f.write("<html><body><p>Reset: <a href='{{reset_link}}'>{{reset_link}}</a></p></body></html>")
        html_path = f.name
    try:
        with patch("smtplib.SMTP") as mock_smtp:
            smtp_instance = MagicMock()
            mock_smtp.return_value.__enter__.return_value = smtp_instance
            send_reset_password_email(
                DummyConfig(), "reset@example.com", "tok456", html_template_path=html_path,
            )
            sent_msg = smtp_instance.send_message.call_args[0][0]
            assert sent_msg.is_multipart()
            html_part = sent_msg.get_body(preferencelist=("html",))
            assert html_part is not None
            assert "tok456" in html_part.get_content()
            # The plain-text alternative must still be present -- best
            # practice, and the fallback if the HTML part is ever dropped.
            text_part = sent_msg.get_body(preferencelist=("plain",))
            assert text_part is not None
            assert "tok456" in text_part.get_content()
    finally:
        os.unlink(html_path)


def test_reset_password_email_missing_html_template_falls_back_to_plain_text():
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = smtp_instance
        # Doesn't raise, doesn't block the send -- degrades to plain-text-only.
        send_reset_password_email(
            DummyConfig(), "reset@example.com", "tok789",
            html_template_path="/nonexistent/path/reset.html",
        )
        sent_msg = smtp_instance.send_message.call_args[0][0]
        assert not sent_msg.is_multipart()
        assert "tok789" in sent_msg.get_content()


def test_reset_password_email_without_html_template_is_plain_only():
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = smtp_instance
        send_reset_password_email(DummyConfig(), "reset@example.com", "tok000")
        sent_msg = smtp_instance.send_message.call_args[0][0]
        assert not sent_msg.is_multipart()


# ── send_email — the generic, non-registration/reset/invite primitive ──────

def test_send_email_plain_text_only():
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = smtp_instance
        send_email(DummyConfig(), "someone@example.com", "Your report is ready", "Plain body text")
        smtp_instance.starttls.assert_called_once()
        smtp_instance.login.assert_called_once_with("user", "pass")
        sent_msg = smtp_instance.send_message.call_args[0][0]
        assert sent_msg["Subject"] == "Your report is ready"
        assert sent_msg["To"] == "someone@example.com"
        assert not sent_msg.is_multipart()
        assert "Plain body text" in sent_msg.get_content()


def test_send_email_with_html_content_sends_multipart():
    with patch("smtplib.SMTP") as mock_smtp:
        smtp_instance = MagicMock()
        mock_smtp.return_value.__enter__.return_value = smtp_instance
        send_email(
            DummyConfig(), "someone@example.com", "Subject", "plain",
            html_content="<html><body><p>styled</p></body></html>",
        )
        sent_msg = smtp_instance.send_message.call_args[0][0]
        assert sent_msg.is_multipart()
        html_part = sent_msg.get_body(preferencelist=("html",))
        assert html_part is not None
        assert "styled" in html_part.get_content()
        text_part = sent_msg.get_body(preferencelist=("plain",))
        assert "plain" in text_part.get_content()


def test_send_email_raises_on_smtp_failure():
    with patch("smtplib.SMTP", side_effect=OSError("connection refused")):
        with pytest.raises(EmailSendError):
            send_email(DummyConfig(), "someone@example.com", "Subject", "plain")
