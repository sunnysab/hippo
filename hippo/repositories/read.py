"""Per-user read state.

Read state belongs to a reader, not to an article: ``articles`` is a shared
table, so the flag lives in its own side table keyed by ``(user_id, article_pk)``
and marking an article read never rewrites a synced row.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from ..utils import utc_now_dt


class ArticleReadRepository:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def set_read(self, *, user_id: int, article_pk: int, read: bool) -> str | None:
        """Mark one article read or unread for one user.

        Idempotent in both directions. Re-reading an already read article keeps
        the original ``read_at``, which is what a "read at" column is for.
        """
        async with self._conn.cursor(row_factory=dict_row) as cur:
            if not read:
                await cur.execute(
                    'DELETE FROM article_read WHERE user_id = %s AND article_pk = %s',
                    (user_id, article_pk),
                )
                return None
            await cur.execute(
                """
                INSERT INTO article_read (user_id, article_pk, read_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, article_pk)
                    DO UPDATE SET read_at = article_read.read_at
                RETURNING read_at
                """,
                (user_id, article_pk, utc_now_dt()),
            )
            row = await cur.fetchone()
        read_at = row.get('read_at') if row else None
        return read_at.isoformat() if read_at else None
