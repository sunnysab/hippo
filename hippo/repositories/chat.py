"""Chat sessions and their messages.

A session with an ``article_pk`` is a reading companion for that article; with
``article_pk`` NULL it is free-form chat. A partial unique index enforces one
article session per user, so reopening an article resumes the same conversation
instead of starting a new one.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from ..utils import utc_now_dt

_SESSION_COLUMNS = 'id, user_id, article_pk, provider_id, title, created_at, updated_at'


def _session_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': row['id'],
        'article_id': row['article_pk'],
        'provider_id': row['provider_id'],
        'title': row['title'],
        'created_at': row['created_at'].isoformat() if row['created_at'] else None,
        'updated_at': row['updated_at'].isoformat() if row['updated_at'] else None,
    }


def _message_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': row['id'],
        'session_id': row['session_id'],
        'role': row['role'],
        'content': row['content'],
        'created_at': row['created_at'].isoformat() if row['created_at'] else None,
    }


class ChatRepository:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def list_sessions(self, user_id: int) -> list[dict[str, Any]]:
        """Sessions first by recency; the sidebar and /chat both want that order."""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_SESSION_COLUMNS} FROM chat_session
                WHERE user_id = %s
                ORDER BY updated_at DESC
                """,
                (user_id,),
            )
            rows = await cur.fetchall()
        return [_session_row(row) for row in rows]

    async def get_session(self, session_id: int, user_id: int) -> dict[str, Any] | None:
        """Fetch one session. The user predicate is the ownership check."""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f'SELECT {_SESSION_COLUMNS} FROM chat_session WHERE id = %s AND user_id = %s',
                (session_id, user_id),
            )
            row = await cur.fetchone()
        return _session_row(row) if row else None

    async def get_session_for_article(self, user_id: int, article_pk: int) -> dict[str, Any] | None:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_SESSION_COLUMNS} FROM chat_session
                WHERE user_id = %s AND article_pk = %s
                """,
                (user_id, article_pk),
            )
            row = await cur.fetchone()
        return _session_row(row) if row else None

    async def create_session(
        self,
        *,
        user_id: int,
        title: str,
        article_pk: int | None = None,
        provider_id: int | None = None,
    ) -> dict[str, Any]:
        now = utc_now_dt()
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO chat_session
                    (user_id, article_pk, provider_id, title, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING {_SESSION_COLUMNS}
                """,
                (user_id, article_pk, provider_id, title, now, now),
            )
            row = await cur.fetchone()
        return _session_row(row)

    async def rename_session(self, session_id: int, user_id: int, title: str) -> bool:
        async with self._conn.cursor() as cur:
            await cur.execute(
                'UPDATE chat_session SET title = %s, updated_at = %s WHERE id = %s AND user_id = %s',
                (title, utc_now_dt(), session_id, user_id),
            )
            return cur.rowcount > 0

    async def delete_session(self, session_id: int, user_id: int) -> bool:
        async with self._conn.cursor() as cur:
            await cur.execute(
                'DELETE FROM chat_session WHERE id = %s AND user_id = %s',
                (session_id, user_id),
            )
            return cur.rowcount > 0

    async def set_provider(self, session_id: int, user_id: int, provider_id: int) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                'UPDATE chat_session SET provider_id = %s, updated_at = %s WHERE id = %s AND user_id = %s',
                (provider_id, utc_now_dt(), session_id, user_id),
            )

    async def list_messages(self, session_id: int, *, limit: int = 200) -> list[dict[str, Any]]:
        """Oldest first: the model needs the conversation in order."""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT id, session_id, role, content, created_at
                FROM chat_message
                WHERE session_id = %s
                ORDER BY id
                LIMIT %s
                """,
                (session_id, max(limit, 1)),
            )
            rows = await cur.fetchall()
        return [_message_row(row) for row in rows]

    async def append_message(self, session_id: int, role: str, content: str) -> dict[str, Any]:
        """Store one turn and touch the session so the list stays ordered."""
        now = utc_now_dt()
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO chat_message (session_id, role, content, created_at)
                VALUES (%s, %s, %s, %s)
                RETURNING id, session_id, role, content, created_at
                """,
                (session_id, role, content, now),
            )
            row = await cur.fetchone()
            await cur.execute(
                'UPDATE chat_session SET updated_at = %s WHERE id = %s',
                (now, session_id),
            )
        return _message_row(row)

    async def find_preset(self, session_id: int, preset: str) -> dict[str, Any] | None:
        """Return an earlier answer produced by a preset action, if any.

        Summaries and key points are derived from the article, not from the
        conversation, so an answer from a previous run is still valid and can be
        served without calling the model again.
        """
        marker = f'[preset:{preset}]'
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT id, session_id, role, content, created_at
                FROM chat_message
                WHERE session_id = %s AND role = 'assistant' AND content LIKE %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (session_id, f'{marker}%'),
            )
            row = await cur.fetchone()
        return _message_row(row) if row else None
