import html
import re
import smtplib
from email.message import EmailMessage
import uuid
from typing import Optional, Protocol
import logging
from database.interface import DatabaseAdapter

logger = logging.getLogger(__name__)


class EmailSendError(RuntimeError):
    """Raised when a transactional email (verify / reset / invite) fails to send.

    #301: the send functions must NOT swallow delivery failures — a caller that
    creates a registration/reset and then reports success while the user never
    receives the link is a silent failure of a security-relevant delivery. Raising
    lets each caller decide (surface an error, roll back, or retry).
    """


# This is a Non-Optional Config union of the Apps actual config
class EmailVerificationConfig(Protocol):
    smtp_user: str
    smtp_password: str
    smtp_host: str
    smtp_port: int
    smtp_from: str

# example of derived union class for SendGrid
# class EmailVerificationConfigSendGrid(EmailVerificationConfig):
#     smtp_type: str = "sendgrid"
#     smtp_key: str
#     smtp_from: str
#     base_url: str
#     verify_email_url: str = "{config.base_url}/verify-email?token={verification_token}"
#     reset_password_url: str = "{config.base_url}/reset-password?token={reset_token}"

# Non-Option Registration definition
class EmailRegistration(Protocol):
    email: str
    verification_token: str
    created: int
    client_base_url: str

def _render_email_content(template: str, **kwargs) -> str:
    """Helper to render email content with provided variables."""
    return template.format(**kwargs)


_HTML_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def _render_html_content(template: str, **kwargs) -> str:
    """Render an HTML email template.

    Deliberately NOT _render_email_content's str.format() — that treats every
    single `{`/`}` as a placeholder, which is fatal for HTML email, where an
    inline <style> block (required by most mail clients, which strip <link>
    stylesheets) is full of literal braces (`body { margin: 0; }`). This only
    ever touches a `{{token}}` pair, so CSS is untouched. A referenced token
    with no matching kwarg raises KeyError -- same "loud on a typo" behavior
    str.format() already has -- and the caller (send_*_email below) catches
    that and falls back to plain-text-only rather than mailing a broken page.

    Every substituted value is HTML-escaped: several of these tokens
    (verification_link/reset_link build from registration.client_base_url,
    which is caller-supplied on the public registration/reset request) would
    otherwise let an attacker break out of an href="..." attribute or inject
    markup into the rendered email.
    """
    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key not in kwargs:
            raise KeyError(key)
        return html.escape(str(kwargs[key]), quote=True)
    return _HTML_PLACEHOLDER_RE.sub(_sub, template)


def _load_html_content(html_template_path: Optional[str], **kwargs) -> Optional[str]:
    """Read + render an optional HTML template file. Returns None (and logs a
    warning) on any failure -- a missing/broken style asset must degrade to
    the plain-text body, not break the send entirely."""
    if not html_template_path:
        return None
    try:
        with open(html_template_path, "r", encoding="utf-8") as f:
            template = f.read()
        return _render_html_content(template, **kwargs)
    except Exception as exc:
        logger.warning(
            "Could not load/render HTML email template %s: %s -- sending plain-text only",
            html_template_path, exc,
        )
        return None

# def send_verification_email(config: EmailVerificationConfig, user_email: str, verification_token: str):
def send_verification_email(
    config: EmailVerificationConfig,
    registration: EmailRegistration,
    *,
    html_template_path: Optional[str] = None,
):
    # verification_link = config.verify_email_url.replace("{verification_token}", registration.verification_token).replace("{config.base_url}", config.base_url)
    if not registration.verification_token:
        raise ValueError("verification_token is required to send verification email")
    if not registration.client_base_url:
        raise ValueError("client_base_url is required to send verification email")
    verification_link = config.verify_email_url.replace(
        "{registration.verification_token}",
        registration.verification_token,
    ).replace(
        "{registration.client_base_url}",
        registration.client_base_url,
    )
    subject = "Email Verification"
    content = _render_email_content(
        "Hi,\n\nPlease verify your email by clicking on the following link:\n{verification_link}\n\nThank you!",
        verification_link=verification_link
    )
    html_content = _load_html_content(html_template_path, verification_link=verification_link)
    if getattr(config, 'smtp_type', 'simple') == 'sendgrid':
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
        mail_kwargs = dict(
            from_email=config.smtp_from,
            to_emails=registration.email,
            subject=subject,
            plain_text_content=content,
        )
        if html_content:
            mail_kwargs["html_content"] = html_content
        message = Mail(**mail_kwargs)
        try:
            sg = SendGridAPIClient(config.smtp_key)
            response = sg.send(message)
            logger.info(f"Verification email sent successfully via SendGrid: {response.status_code}")
        except Exception as e:
            logger.error(f"Failed to send verification email via SendGrid: {e}")
            raise EmailSendError(f"verification email to {registration.email} failed via SendGrid") from e
        return
    # SMTP fallback
    msg = EmailMessage()
    msg.set_content(content)
    if html_content:
        msg.add_alternative(html_content, subtype="html")
    msg['Subject'] = subject
    msg['From'] = config.smtp_from
    msg['To'] = registration.email
    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(config.smtp_user, config.smtp_password)
            smtp.send_message(msg)
        logger.info("Verification email sent successfully")
    except Exception as e:
        logger.error(f"Failed to send verification email: {e}")
        raise EmailSendError(f"verification email to {registration.email} failed via SMTP") from e

def send_invite_email(
    config: EmailVerificationConfig,
    user_email: str,
    accept_url: str,
    *,
    inviter_name: str = None,
    org_name: str = None,
    html_template_path: Optional[str] = None,
):
    """Send a staff invitation email (#310). ``accept_url`` is the fully-built
    single-use link the invitee follows to set their password and activate
    their account. Mirrors send_reset_password_email's SendGrid/SMTP handling."""
    if not accept_url:
        raise ValueError("accept_url is required to send an invite email")
    who = inviter_name or "An administrator"
    org = f" at {org_name}" if org_name else ""
    subject = f"You've been invited{org}"
    content = _render_email_content(
        "Hi,\n\n{who} has invited you to join{org}.\n\n"
        "Set your password and activate your account here:\n{accept_url}\n\n"
        "This invitation link expires in a few days. If you weren't expecting "
        "this, you can ignore this email.\n\nThank you!",
        who=who, org=org, accept_url=accept_url,
    )
    html_content = _load_html_content(html_template_path, who=who, org=org, accept_url=accept_url)
    if getattr(config, 'smtp_type', 'simple') == 'sendgrid':
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
        mail_kwargs = dict(
            from_email=config.smtp_from,
            to_emails=user_email,
            subject=subject,
            plain_text_content=content,
        )
        if html_content:
            mail_kwargs["html_content"] = html_content
        message = Mail(**mail_kwargs)
        try:
            sg = SendGridAPIClient(config.smtp_key)
            response = sg.send(message)
            logger.info(f"Invite email sent successfully via SendGrid: {response.status_code}")
        except Exception as e:
            logger.error(f"Failed to send invite email via SendGrid: {e}")
            raise EmailSendError(f"invite email to {user_email} failed via SendGrid") from e
        return
    # SMTP fallback
    msg = EmailMessage()
    msg.set_content(content)
    if html_content:
        msg.add_alternative(html_content, subtype="html")
    msg['Subject'] = subject
    msg['From'] = config.smtp_from
    msg['To'] = user_email
    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(config.smtp_user, config.smtp_password)
            smtp.send_message(msg)
        logger.info("Invite email sent successfully")
    except Exception as e:
        logger.error(f"Failed to send invite email: {e}")
        raise EmailSendError(f"invite email to {user_email} failed via SMTP") from e

def send_reset_password_email(
    config: EmailVerificationConfig,
    user_email: str,
    reset_token: str,
    *,
    html_template_path: Optional[str] = None,
):
    # base_url is legitimately unset in local/dev configs (the request's own
    # scheme://host is used elsewhere in the app); falling back to "" here
    # turns that into a relative, still-templated link instead of a
    # TypeError from str.replace(..., None).
    reset_link = config.reset_password_url.replace("{reset_token}", reset_token).replace(
        "{config.base_url}", getattr(config, "base_url", None) or ""
    )
    subject = "Password Reset Request"
    content = _render_email_content(
        "Hi,\n\nYou can reset your password by clicking on the following link:\n{reset_link}\n\nIf you did not request this, please ignore this email.\n\nThank you!",
        reset_link=reset_link
    )
    html_content = _load_html_content(html_template_path, reset_link=reset_link)
    if getattr(config, 'smtp_type', 'simple') == 'sendgrid':
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
        mail_kwargs = dict(
            from_email=config.smtp_from,
            to_emails=user_email,
            subject=subject,
            plain_text_content=content,
        )
        if html_content:
            mail_kwargs["html_content"] = html_content
        message = Mail(**mail_kwargs)
        try:
            sg = SendGridAPIClient(config.smtp_key)
            response = sg.send(message)
            logger.info(f"Password reset email sent successfully via SendGrid: {response.status_code}")
        except Exception as e:
            logger.error(f"Failed to send password reset email via SendGrid: {e}")
            raise EmailSendError(f"password reset email to {user_email} failed via SendGrid") from e
        return
    # SMTP fallback
    msg = EmailMessage()
    msg.set_content(content)
    if html_content:
        msg.add_alternative(html_content, subtype="html")
    msg['Subject'] = subject
    msg['From'] = config.smtp_from
    msg['To'] = user_email
    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(config.smtp_user, config.smtp_password)
            smtp.send_message(msg)
        logger.info("Password reset email sent successfully")
    except Exception as e:
        logger.error(f"Failed to send password reset email: {e}")
        raise EmailSendError(f"password reset email to {user_email} failed via SMTP") from e


def send_email(
    config: EmailVerificationConfig,
    to_email: str,
    subject: str,
    plain_text_content: str,
    *,
    html_content: Optional[str] = None,
) -> None:
    """Generic transactional email — same SMTP/SendGrid branching as
    send_verification_email/send_reset_password_email/send_invite_email above,
    factored out for any email that isn't a registration/reset/invite flow
    (e.g. a report-completion notification) so it doesn't need its own copy
    of this logic.

    Unlike the other three, this takes already-rendered html_content (a
    string) rather than an html_template_path — callers with custom
    per-email fields render their own template first (see
    _load_html_content/_render_html_content above, reusable directly) and
    pass the result straight through.
    """
    if getattr(config, 'smtp_type', 'simple') == 'sendgrid':
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
        mail_kwargs = dict(
            from_email=config.smtp_from,
            to_emails=to_email,
            subject=subject,
            plain_text_content=plain_text_content,
        )
        if html_content:
            mail_kwargs["html_content"] = html_content
        message = Mail(**mail_kwargs)
        try:
            sg = SendGridAPIClient(config.smtp_key)
            response = sg.send(message)
            logger.info(f"Email sent successfully via SendGrid: {response.status_code}")
        except Exception as e:
            logger.error(f"Failed to send email via SendGrid: {e}")
            raise EmailSendError(f"email to {to_email} failed via SendGrid") from e
        return
    # SMTP fallback
    msg = EmailMessage()
    msg.set_content(plain_text_content)
    if html_content:
        msg.add_alternative(html_content, subtype="html")
    msg['Subject'] = subject
    msg['From'] = config.smtp_from
    msg['To'] = to_email
    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(config.smtp_user, config.smtp_password)
            smtp.send_message(msg)
        logger.info("Email sent successfully")
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        raise EmailSendError(f"email to {to_email} failed via SMTP") from e
