"""auth package."""

from .interface import AuthProvider
from .factory import get_auth_provider
from .auth_util import AuthDependencies, create_auth_dependencies, no_auth_required, no_role_required
from .local_auth import LocalAuthProvider
from .none_auth import NoneAuthProvider
from .oauth_auth import OAuthAuthProvider
from .jwt_util import (
    assert_jwt_secret_configured,
    get_jwt_secret,
    is_weak_jwt_secret,
    FALLBACK_JWT_SECRET,
)

__version__ = "0.1.0"

__all__ = [
    "AuthProvider",
    "get_auth_provider",
    "AuthDependencies",
    "create_auth_dependencies",
    "no_auth_required",
    "no_role_required",
    "LocalAuthProvider",
    "NoneAuthProvider",
    "OAuthAuthProvider",
    "assert_jwt_secret_configured",
    "get_jwt_secret",
    "is_weak_jwt_secret",
    "FALLBACK_JWT_SECRET",
]
