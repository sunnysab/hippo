"""Password hashing and opaque-token helpers.

bcrypt is deliberately CPU-heavy (roughly 200-300 ms per call at the default
cost), so API handlers must run these through ``asyncio.to_thread``.
"""

from __future__ import annotations

import hashlib
import secrets

import bcrypt

#: Session and e-mail verification tokens are stored hashed, never in clear.
_TOKEN_BYTES = 32


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('ascii')


def verify_password(password: str, hashed: str) -> bool:
    """Check ``password`` against a stored bcrypt hash."""
    if not password or not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('ascii'))
    except ValueError, TypeError:
        return False


def new_token() -> str:
    """Return a fresh URL-safe token to hand to the client."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def token_hash(token: str) -> str:
    """Return the lookup hash for a token. Leaves the raw value only in the cookie."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


__all__ = ['hash_password', 'new_token', 'token_hash', 'verify_password']
