from abc import ABC, abstractmethod
from typing import Optional

from . import password_util

class AuthProvider(ABC):
    """Abstract interface for authentication providers."""

    def generate_hash(self, password: str) -> str:
        """Hash a password with scrypt (per-user salt + work factor). (#269)"""
        return password_util.hash_password(password)

    def verify_password(self, password: str, password_hash: str) -> bool:
        """Constant-time verify supporting scrypt and legacy SHA-256 hashes."""
        return password_util.verify_password(password, password_hash)

    def should_rehash_password(self, password_hash: str) -> bool:
        """True when a stored hash is legacy (SHA-256) and should be upgraded."""
        return password_util.should_rehash(password_hash)

    @abstractmethod
    def register_user(self, username: str, password: str = None, password_hash: str = None) -> dict:
        """Register a new user."""
        pass

    @abstractmethod
    def authenticate(self, username: str, password: str, in_token: str = None) -> Optional[str]:
        """Authenticate a user and return a token if successful."""
        pass

    @abstractmethod
    def get_user(self, token: str) -> Optional[dict]:
        """Retrieve user details from token."""
        pass

    @abstractmethod
    def refresh_token(self, refresh_token: str) -> Optional[str]:
        """Refresh an authentication token."""
        pass

    @abstractmethod
    def logout(self, token: str) -> bool:
        """Logout the user (invalidate token)."""
        pass

    @abstractmethod
    def generate_token(self, user: dict, *, ttl_minutes: Optional[int] = None) -> str:
        """Mint an HS256 access-token JWT for the given user dict.

        ``ttl_minutes`` overrides the package default. server.py supplies the
        value from the ``on_get_jwt_access_token_ttl_minutes`` event-handler hook,
        so each app/deployment can tune access-token lifetime independently.
        """
        pass
