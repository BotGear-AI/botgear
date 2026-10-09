# mfa.py
import io
from fastapi.responses import StreamingResponse

def generate_totp_secret(username: str) -> str:
    import pyotp  # Dynamically import the pyotp library
    return pyotp.random_base32()

def get_totp_uri(username: str, secret: str, issuer: str | None = "App") -> str:
    import pyotp  # Dynamically import the pyotp library
    # `issuer or "App"`, not a bare `issuer`. Callers commonly pass a
    # settings-derived base_url into this slot, and that setting can default
    # to None on a deployment that hasn't configured it. An explicit None
    # does NOT fall back to the default in the signature above: it reaches
    # pyotp, which then drops the issuer from the URI entirely —
    # `otpauth://totp/alice%40x.com?secret=...`, no issuer label and no
    # issuer parameter — so the authenticator app lists a bare user id under
    # no application name.
    return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer or "App")

def generate_qr_code(uri: str) -> StreamingResponse:
    import qrcode  # Dynamically import the qrcode library
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")

def verify_totp(secret: str, code: int, valid_window: int = 1) -> bool:
    # valid_window=1 accepts only ±1 30s step for clock skew (pyotp's own default).
    # The previous 5 accepted an ~5.5-minute band of codes (#294), ~11x the
    # brute-force/replay surface.
    import pyotp  # Dynamically import the pyotp library
    return pyotp.totp.TOTP(secret).verify(code, valid_window=valid_window)