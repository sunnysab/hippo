"""Server-side sessions.

Kept in Postgres rather than a signed cookie so an admin can revoke a session
and see who is currently signed in.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from ..models import User
from ..security import new_token, token_hash
from ..utils import utc_now_dt
from .user import _row_to_user

_USER_COLUMNS = 'u.id, u.username, u.email, u.email_verified, u.role, u.timezone, u.is_disabled'

_DEFAULT_TTL_HOURS = 24 * 7


def session_ttl() -> timedelta:
    raw = os.environ.get('HIPPO_SESSION_TTL_HOURS', '').strip()
    try:
        hours = float(raw)
    except ValueError:
        hours = _DEFAULT_TTL_HOURS
    return timedelta(hours=max(hours, 1.0))


class UserSessionRepository:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def issue(
        self,
        user_id: int,
        *,
        user_agent: str | None = None,
        ip: str | None = None,
        ttl: timedelta | None = None,
    ) -> str:
        """Create a session row and return the raw token for the cookie.

        ``ttl`` overrides the default lifetime; RSS readers get a much longer
        one because they cannot re-authenticate interactively.
        """
        token = new_token()
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO user_session (token_hash, user_id, user_agent, ip, created_at, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (token_hash(token), user_id, user_agent, ip, now, now + (ttl or session_ttl())),
            )
        return token

    async def resolve(self, token: str) -> User | None:
        """Return the user behind a live, non-expired session."""
        if not token:
            return None
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_USER_COLUMNS}
                FROM user_session s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = %s AND s.expires_at > %s AND u.is_disabled = FALSE
                """,
                (token_hash(token), utc_now_dt()),
            )
            row = await cur.fetchone()
        return _row_to_user(row) if row else None

    async def revoke(self, token: str) -> bool:
        async with self._conn.cursor() as cur:
            await cur.execute('DELETE FROM user_session WHERE token_hash = %s', (token_hash(token),))
            return (cur.rowcount or 0) > 0

    async def revoke_all_for_user(self, user_id: int) -> int:
        async with self._conn.cursor() as cur:
            await cur.execute('DELETE FROM user_session WHERE user_id = %s', (user_id,))
            return cur.rowcount or 0

    async def list_for_user(self, user_id: int) -> list[dict[str, Any]]:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT token_hash, user_agent, ip, created_at, expires_at
                FROM user_session WHERE user_id = %s ORDER BY created_at DESC
                """,
                (user_id,),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def count_for_user(self, user_id: int) -> int:
        async with self._conn.cursor() as cur:
            await cur.execute('SELECT count(*) FROM user_session WHERE user_id = %s', (user_id,))
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def purge_expired(self) -> int:
        async with self._conn.cursor() as cur:
            await cur.execute('DELETE FROM user_session WHERE expires_at <= %s', (utc_now_dt(),))
            return cur.rowcount or 0
