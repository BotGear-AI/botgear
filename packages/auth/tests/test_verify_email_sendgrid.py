"""SendGrid paths, invite email, and input guards for auth.verify_email.

sendgrid is not installed in the test venv; a fake module is injected into
sys.modules so the lazy `from sendgrid import ...` inside each sender resolves.
"""
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from auth.verify_email import (
    EmailSendError,
    send_email,
    send_invite_email,
    send_reset_password_email,
    send_verification_email,
)


class SendGridConfig:
    smtp_type = "sendgrid"
    smtp_key = "SG.test-key"
    smtp_from = "noreply@example.com"
    smtp_user = smtp_password = smtp_host = ""
    smtp_port = 0
    base_url = "https://app.example.com"
    verify_email_url = "{registration.client_base_url}/verify?token={registration.verification_token}"
    reset_password_url = "{config.base_url}/reset?token={reset_token}"


class SmtpConfig(SendGridConfig):
    smtp_type = "simple"
    smtp_user = "user"
    smtp_password = "pass"
    smtp_host = "smtp.example.com"
    smtp_port = 587


def _registration(**overrides):
    fields = {
        "email": "new@example.com", "verification_token": "abc123",
        "created": 1, "client_base_url": "http://localhost:3000",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture
def sendgrid(monkeypatch):
    state = SimpleNamespace(sent=[], fail=None)

    class FakeClient:
        def __init__(self, key):
            self.key = key

        def send(self, message):
            if state.fail is not None:
                raise state.fail
            state.sent.append((self.key, message.kwargs))
            return SimpleNamespace(status_code=202)

    class FakeMail:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    pkg = types.ModuleType("sendgrid")
    helpers = types.ModuleType("sendgrid.helpers")
    mail = types.ModuleType("sendgrid.helpers.mail")
    pkg.SendGridAPIClient = FakeClient
    pkg.helpers = helpers
    helpers.mail = mail
    mail.Mail = FakeMail
    monkeypatch.setitem(sys.modules, "sendgrid", pkg)
    monkeypatch.setitem(sys.modules, "sendgrid.helpers", helpers)
    monkeypatch.setitem(sys.modules, "sendgrid.helpers.mail", mail)
    with patch("smtplib.SMTP") as smtp:
        yield state
    smtp.assert_not_called()


@pytest.fixture
def html_template(tmp_path):
    path = tmp_path / "email.html"
    path.write_text(
        "<style>p { margin: 0; }</style>"
        "<p>{{verification_link}}|{{reset_link}}|{{accept_url}}|{{who}}</p>"
    )
    return str(path)


SENDERS = {
    "verification": (
        lambda cfg, **kw: send_verification_email(cfg, _registration(), **kw),
        "new@example.com", "Email Verification", "http://localhost:3000/verify?token=abc123",
    ),
    "reset": (
        lambda cfg, **kw: send_reset_password_email(cfg, "reset@example.com", "tok1", **kw),
        "reset@example.com", "Password Reset Request", "https://app.example.com/reset?token=tok1",
    ),
    "invite": (
        lambda cfg, **kw: send_invite_email(
            cfg, "invitee@example.com", "https://app.example.com/accept?t=9",
            inviter_name="Pat", org_name="Acme", **kw),
        "invitee@example.com", "You've been invited at Acme", "Pat has invited you to join at Acme.",
    ),
}


@pytest.mark.parametrize("name", SENDERS)
def test_sendgrid_plain_text_send(sendgrid, name):
    send, to_email, subject, body_fragment = SENDERS[name]
    send(SendGridConfig())

    [(key, mail)] = sendgrid.sent
    assert key == "SG.test-key"
    assert mail["from_email"] == "noreply@example.com"
    assert mail["to_emails"] == to_email
    assert mail["subject"] == subject
    assert body_fragment in mail["plain_text_content"]
    assert "html_content" not in mail


def test_sendgrid_html_templates_are_rendered_and_attached(sendgrid, tmp_path):
    for name, (send, *_rest) in SENDERS.items():
        template = tmp_path / f"{name}.html"
        placeholder = {"verification": "verification_link", "reset": "reset_link", "invite": "accept_url"}[name]
        template.write_text(f"<style>a {{ color: red; }}</style><a href='{{{{{placeholder}}}}}'>go</a>")
        send(SendGridConfig(), html_template_path=str(template))

    htmls = [mail["html_content"] for _, mail in sendgrid.sent]
    assert len(htmls) == 3
    assert "href='http://localhost:3000/verify?token=abc123'" in htmls[0]
    assert "href='https://app.example.com/reset?token=tok1'" in htmls[1]
    assert "href='https://app.example.com/accept?t=9'" in htmls[2]
    assert all("a { color: red; }" in h for h in htmls)


@pytest.mark.parametrize("name", SENDERS)
def test_sendgrid_failure_raises_email_send_error(sendgrid, name):
    send, to_email, *_ = SENDERS[name]
    sendgrid.fail = ConnectionError("sendgrid down")
    with pytest.raises(EmailSendError, match="via SendGrid") as excinfo:
        send(SendGridConfig())
    assert to_email in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ConnectionError)


def test_send_email_via_sendgrid_with_and_without_html(sendgrid):
    send_email(SendGridConfig(), "a@example.com", "Report", "plain body")
    send_email(SendGridConfig(), "b@example.com", "Report", "plain body", html_content="<p>hi</p>")

    (_, plain), (_, rich) = sendgrid.sent
    assert plain["to_emails"] == "a@example.com" and "html_content" not in plain
    assert rich["html_content"] == "<p>hi</p>"
    assert rich["plain_text_content"] == "plain body"


def test_send_email_sendgrid_failure(sendgrid):
    sendgrid.fail = RuntimeError("boom")
    with pytest.raises(EmailSendError, match="email to a@example.com failed via SendGrid"):
        send_email(SendGridConfig(), "a@example.com", "Report", "plain body")


@pytest.mark.parametrize(
    "overrides, message",
    [({"verification_token": ""}, "verification_token"), ({"client_base_url": None}, "client_base_url")],
)
def test_verification_email_requires_token_and_base_url(overrides, message):
    with patch("smtplib.SMTP") as smtp:
        with pytest.raises(ValueError, match=message):
            send_verification_email(SmtpConfig(), _registration(**overrides))
    smtp.assert_not_called()


def _smtp_capture():
    smtp_instance = MagicMock()
    patcher = patch("smtplib.SMTP")
    mock_smtp = patcher.start()
    mock_smtp.return_value.__enter__.return_value = smtp_instance
    return patcher, mock_smtp, smtp_instance


def test_verification_email_smtp_with_html_template(html_template):
    patcher, mock_smtp, smtp = _smtp_capture()
    try:
        # The template references tokens this sender does not supply, so the
        # render fails and the send degrades to plain text instead of raising.
        send_verification_email(SmtpConfig(), _registration(), html_template_path=html_template)
        assert not smtp.send_message.call_args.args[0].is_multipart()

        template = html_template.replace("email.html", "verify.html")
        with open(template, "w") as f:
            f.write("<a href='{{verification_link}}'>verify</a>")
        send_verification_email(SmtpConfig(), _registration(), html_template_path=template)
        msg = smtp.send_message.call_args.args[0]
        assert msg.is_multipart()
        assert "token=abc123" in msg.get_body(preferencelist=("html",)).get_content()
        mock_smtp.assert_called_with("smtp.example.com", 587, timeout=10)
    finally:
        patcher.stop()


def test_invite_email_smtp_defaults_and_html(tmp_path):
    template = tmp_path / "invite.html"
    template.write_text("<p>{{who}}{{org}} <a href='{{accept_url}}'>accept</a></p>")
    patcher, _, smtp = _smtp_capture()
    try:
        send_invite_email(SmtpConfig(), "inv@example.com", "https://x/accept")
        msg = smtp.send_message.call_args.args[0]
        assert msg["Subject"] == "You've been invited"
        assert msg["To"] == "inv@example.com"
        assert "An administrator has invited you to join." in msg.get_content()
        assert "https://x/accept" in msg.get_content()
        smtp.login.assert_called_with("user", "pass")

        send_invite_email(SmtpConfig(), "inv@example.com", "https://x/accept",
                          org_name="Acme", html_template_path=str(template))
        msg = smtp.send_message.call_args.args[0]
        assert msg.is_multipart()
        assert "An administrator at Acme" in msg.get_body(preferencelist=("html",)).get_content()
    finally:
        patcher.stop()


def test_invite_email_requires_accept_url():
    with pytest.raises(ValueError, match="accept_url"):
        send_invite_email(SmtpConfig(), "inv@example.com", "")


def test_invite_email_smtp_failure_raises():
    with patch("smtplib.SMTP", side_effect=OSError("refused")):
        with pytest.raises(EmailSendError, match="invite email to inv@example.com failed via SMTP"):
            send_invite_email(SmtpConfig(), "inv@example.com", "https://x/accept")
