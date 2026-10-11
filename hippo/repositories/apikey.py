"""API keys for the public read API.

The secret is only ever shown once, at creation: the table keeps a hash and a short
prefix, exactly like the session tokens do. That means the API layer can list keys
with a display prefix, and a database dump cannot be replayed against the API.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

#: Scheme prefix, so a leaked string is recognisable in logs.
KEY_SCHEME = 'hippo_'
_SECRET_BYTES = 24
#: How much of the secret is kept in clear for display ("hippo_a1b2c3d4…").
_VISIBLE_CHARS = 8

#: Scopes a key can carry. ``read`` covers every GET; ``write`` is not issued by the
#: console yet, but the column exists so widening a key stays a data change.
SCOPES = ('read', 'write')

#: Rewriting last_used_at on every request would be a write per read. Once per
#: window is enough to answer "is this key still in use".
_TOUCH_INTERVAL = '5 minutes'


def generate_key() -> str:
    return f'{KEY_SCHEME}{secrets.token_urlsafe(_SECRET_BYTES)}'


def key_prefix(secret: str) -> str:
    """The display prefix: enough to recognise a key, not enough to use it."""
    body = secret[len(KEY_SCHEME) :] if secret.startswith(KEY_SCHEME) else secret
    return body[:_VISIBLE_CHARS]


def key_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode('utf-8')).hexdigest()


def _row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': int(row['id']),
        'name': str(row['name']),
        'prefix': str(row['prefix']),
        'scopes': list(row['scopes'] or []),
        'last_used_at': row['last_used_at'].isoformat() if row['last_used_at'] else None,
        'last_used_ip': row['last_used_ip'],
        'expires_at': row['expires_at'].isoformat() if row['expires_at'] else None,
        'created_at': row['created_at'].isoformat() if row['created_at'] else None,
    }


class ApiKeyRepository:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def create(
        self,
        user_id: int,
        *,
        name: str,
        scopes: tuple[str, ...] = ('read',),
        expires_at: datetime | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Create a key and return ``(record, secret)`` — the only time the secret exists."""
        secret = generate_key()
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO api_key (user_id, name, prefix, key_hash, scopes, expires_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                RETURNING id, name, prefix, scopes, expires_at, created_at
                """,
                (int(user_id), name[:80], key_prefix(secret), key_hash(secret), list(scopes), expires_at),
            )
            row = await cur.fetchone()
        record = _row({**dict(row), 'last_used_at': None, 'last_used_ip': None})
        return record, secret

    async def list_for_user(self, user_id: int) -> list[dict[str, Any]]:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT id, name, prefix, scopes, last_used_at, last_used_ip, expires_at, created_at
                  FROM api_key
                 WHERE user_id = %s AND revoked_at IS NULL
                 ORDER BY id DESC
                """,
                (int(user_id),),
            )
            return [_row(dict(row)) for row in await cur.fetchall()]

    async def revoke(self, user_id: int, key_id: int) -> bool:
        """Revoke one of the caller's own keys. Returns whether a row changed."""
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE api_key SET revoked_at = NOW()
                 WHERE id = %s AND user_id = %s AND revoked_at IS NULL
                """,
                (int(key_id), int(user_id)),
            )
            return cur.rowcount > 0

    async def authenticate(self, secret: str) -> dict[str, Any] | None:
        """Resolve a secret to its key, or ``None`` when unusable."""
        if not secret or not secret.startswith(KEY_SCHEME):
            return None
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT id, user_id, name, scopes, expires_at
                  FROM api_key
                 WHERE key_hash = %s
                   AND revoked_at IS NULL
                   AND (expires_at IS NULL OR expires_at > NOW())
                """,
                (key_hash(secret),),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def touch(self, key_id: int, ip: str | None) -> None:
        """Record use, at most once per window."""
        async with self._conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE api_key SET last_used_at = NOW(), last_used_ip = %s
                 WHERE id = %s
                   AND (last_used_at IS NULL OR last_used_at < NOW() - INTERVAL '{_TOUCH_INTERVAL}')
                """,
                (ip, int(key_id)),
            )


__all__ = ['KEY_SCHEME', 'SCOPES', 'ApiKeyRepository', 'generate_key', 'key_hash', 'key_prefix']
