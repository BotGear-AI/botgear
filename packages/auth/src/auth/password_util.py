"""Password hashing utilities (#269).

Uses stdlib ``hashlib.scrypt`` — a memory-hard KDF with a per-user random salt
and a work factor — so no compiled third-party dependency is added to the 13
apps that consume this package.

- ``hash_password``  -> new scrypt hash, format ``scrypt$N$r$p$<salt_hex>$<dk_hex>``
- ``verify_password`` -> constant-time verify; accepts scrypt hashes AND legacy
  unsalted SHA-256 (64-hex) hashes so existing stored credentials keep working.
- ``should_rehash``  -> True for legacy SHA-256 hashes, enabling transparent
  upgrade-on-login (rehash to scrypt after a successful auth).
"""
import hashlib
import hmac
import logging
import os

logger = logging.getLogger(__name__)

# scrypt parameters (RFC 7914). N must be a power of two. These give ~70ms/hash.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_PREFIX = "scrypt"


def hash_password(password: str) -> str:
    """Hash a plaintext password with scrypt and a fresh random salt."""
    if not isinstance(password, str):
        raise ValueError("password must be a string")
    salt = os.urandom(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=0,
    )
    return f"{_SCRYPT_PREFIX}${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def _looks_like_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against a scrypt or legacy SHA-256 hash."""
    if not isinstance(password, str) or not isinstance(password_hash, str) or not password_hash:
        return False

    if password_hash.startswith(_SCRYPT_PREFIX + "$"):
        try:
            _, n, r, p, salt_hex, dk_hex = password_hash.split("$")
            expected = bytes.fromhex(dk_hex)
            dk = hashlib.scrypt(
                password.encode("utf-8"),
                salt=bytes.fromhex(salt_hex),
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=len(expected),
                maxmem=0,
            )
            return hmac.compare_digest(dk, expected)
        except Exception as exc:  # malformed stored hash
            logger.warning("scrypt verify failed: %s", exc)
            return False

    # Legacy unsalted SHA-256 (pre-#269) — verified for backward compatibility
    # and flagged by should_rehash() for upgrade-on-login.
    if _looks_like_sha256(password_hash):
        computed = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(computed, password_hash.lower())

    logger.warning("unknown password hash format; rejecting")
    return False


def should_rehash(password_hash: str) -> bool:
    """True when a stored hash uses a legacy algorithm and should be upgraded."""
    if not isinstance(password_hash, str) or not password_hash:
        return False
    if password_hash.startswith(_SCRYPT_PREFIX + "$"):
        return False
    return _looks_like_sha256(password_hash)
