"""Single-use tokens for e-mail verification and password reset.

One table serves both flows: they differ only in ``kind`` and in the message
that carries the link.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

import psycopg

from ..security import new_token, token_hash
from ..utils import utc_now_dt

VERIFY_EMAIL: Final = 'verify_email'
RESET_PASSWORD: Final = 'reset_password'

VERIFY_TTL: Final = timedelta(hours=24)
RESET_TTL: Final = timedelta(hours=1)


class UserTokenRepository:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def issue(self, user_id: int, kind: str, *, ttl: timedelta) -> str:
        """Invalidate previous tokens of this kind and return a fresh one."""
        now = utc_now_dt()
        token = new_token()
        async with self._conn.cursor() as cur:
            # Only the newest link should work; otherwise an old mail stays
            # valid after the user requests a second one.
            await cur.execute(
                'DELETE FROM user_token WHERE user_id = %s AND kind = %s AND used_at IS NULL',
                (user_id, kind),
            )
            await cur.execute(
                """
                INSERT INTO user_token (user_id, kind, token_hash, expires_at, used_at)
                VALUES (%s, %s, %s, %s, NULL)
                """,
                (user_id, kind, token_hash(token), now + ttl),
            )
        return token

    async def consume(self, token: str, kind: str) -> int | None:
        """Mark the token used and return its user id, or ``None`` if invalid."""
        if not token:
            return None
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE user_token SET used_at = %s
                WHERE token_hash = %s AND kind = %s AND used_at IS NULL AND expires_at > %s
                RETURNING user_id
                """,
                (now, token_hash(token), kind, now),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else None

    async def purge_expired(self) -> int:
        async with self._conn.cursor() as cur:
            await cur.execute('DELETE FROM user_token WHERE expires_at <= %s', (utc_now_dt(),))
            return cur.rowcount or 0
