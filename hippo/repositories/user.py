"""User accounts.

Everything user-scoped hangs off this table: subscriptions, read state,
annotations, chat sessions and report settings.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from ..models import User
from ..utils import utc_now_dt

_USER_COLUMNS = 'id, username, email, email_verified, role, timezone, is_disabled'

_DEFAULT_TIMEZONE = 'Asia/Shanghai'


def _row_to_user(row: dict[str, Any]) -> User:
    return User(
        id=row['id'],
        username=row['username'],
        email=row['email'],
        email_verified=bool(row['email_verified']),
        role=row['role'],
        timezone=row['timezone'],
        is_disabled=bool(row['is_disabled']),
    )


class UserRepository:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def create(
        self,
        *,
        username: str,
        password_hash: str,
        email: str | None = None,
        role: str = 'user',
        email_verified: bool = False,
        timezone: str = _DEFAULT_TIMEZONE,
    ) -> User:
        now = utc_now_dt()
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO users
                    (username, email, email_verified, password_hash, role, timezone,
                     is_disabled, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, FALSE, %s, %s)
                RETURNING {_USER_COLUMNS}
                """,
                (username, email, email_verified, password_hash, role, timezone, now, now),
            )
            row = await cur.fetchone()
        return _row_to_user(row)

    async def get(self, user_id: int) -> User | None:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f'SELECT {_USER_COLUMNS} FROM users WHERE id = %s', (user_id,))
            row = await cur.fetchone()
        return _row_to_user(row) if row else None

    async def get_with_password(self, username: str) -> tuple[User, str] | None:
        """Return ``(user, password_hash)`` for login, or ``None``."""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f'SELECT {_USER_COLUMNS}, password_hash FROM users WHERE username = %s',
                (username,),
            )
            row = await cur.fetchone()
        if not row:
            return None
        return _row_to_user(row), row['password_hash']

    async def get_by_email(self, email: str) -> User | None:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f'SELECT {_USER_COLUMNS} FROM users WHERE lower(email) = lower(%s)',
                (email,),
            )
            row = await cur.fetchone()
        return _row_to_user(row) if row else None

    async def list_all(self) -> list[User]:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f'SELECT {_USER_COLUMNS} FROM users ORDER BY id')
            rows = await cur.fetchall()
        return [_row_to_user(row) for row in rows]

    async def list_with_stats(self) -> list[dict[str, Any]]:
        """Every user plus live-session count and last sign-in, for the admin list.

        Last sign-in is derived from the session table: a dedicated column would
        need a write on every request for a value only this page reads.
        """
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT u.id, u.username, u.email, u.email_verified, u.role,
                       u.timezone, u.is_disabled, u.created_at,
                       count(s.token_hash) AS session_count,
                       max(s.created_at) AS last_login_at
                FROM users u
                LEFT JOIN user_session s
                       ON s.user_id = u.id AND s.expires_at > now()
                GROUP BY u.id
                ORDER BY u.id
                """
            )
            rows = await cur.fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            record['email_verified'] = bool(record['email_verified'])
            record['is_disabled'] = bool(record['is_disabled'])
            record['session_count'] = int(record['session_count'])
            for key in ('created_at', 'last_login_at'):
                value = record.get(key)
                record[key] = value.isoformat() if value else None
            result.append(record)
        return result

    async def count(self) -> int:
        async with self._conn.cursor() as cur:
            await cur.execute('SELECT count(*) FROM users')
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def any_admin_exists(self) -> bool:
        async with self._conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1")
            return await cur.fetchone() is not None

    async def set_password(self, user_id: int, password_hash: str) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                'UPDATE users SET password_hash = %s, updated_at = %s WHERE id = %s',
                (password_hash, utc_now_dt(), user_id),
            )

    async def set_disabled(self, user_id: int, is_disabled: bool) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                'UPDATE users SET is_disabled = %s, updated_at = %s WHERE id = %s',
                (is_disabled, utc_now_dt(), user_id),
            )

    async def set_role(self, user_id: int, role: str) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                'UPDATE users SET role = %s, updated_at = %s WHERE id = %s',
                (role, utc_now_dt(), user_id),
            )

    async def set_email_verified(self, user_id: int, verified: bool = True) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                'UPDATE users SET email_verified = %s, updated_at = %s WHERE id = %s',
                (verified, utc_now_dt(), user_id),
            )

    async def set_timezone(self, user_id: int, timezone: str) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                'UPDATE users SET timezone = %s, updated_at = %s WHERE id = %s',
                (timezone, utc_now_dt(), user_id),
            )

    async def get_preferences(self, user_id: int) -> dict[str, Any]:
        """Per-user reading preferences (article filters, reader defaults)."""
        async with self._conn.cursor() as cur:
            await cur.execute('SELECT preferences FROM users WHERE id = %s', (user_id,))
            row = await cur.fetchone()
        if not row or row[0] is None:
            return {}
        value = row[0]
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return {}
        return value if isinstance(value, dict) else {}

    async def set_preferences(self, user_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        """Merge ``updates`` into the user's preferences and return the result."""
        current = await self.get_preferences(user_id)
        current.update(updates)
        async with self._conn.cursor() as cur:
            await cur.execute(
                'UPDATE users SET preferences = %s, updated_at = %s WHERE id = %s',
                (Json(current), utc_now_dt(), user_id),
            )
        return current
