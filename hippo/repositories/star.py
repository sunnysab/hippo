"""Per-user stars.

A star belongs to a reader, not to an article: ``articles`` is a shared table,
so the flag lives in its own side table keyed by ``(user_id, article_pk)``. The
table mirrors ``article_read``; keeping them apart means neither feature has to
reason about the other's rows.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from ..utils import utc_now_dt


class ArticleStarRepository:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def set_starred(self, *, user_id: int, article_pk: int, starred: bool) -> str | None:
        """Star or unstar one article for one user.

        Idempotent in both directions. Starring twice keeps the first
        ``starred_at``, the same way re-reading keeps the first ``read_at``.
        """
        async with self._conn.cursor(row_factory=dict_row) as cur:
            if not starred:
                await cur.execute(
                    'DELETE FROM article_star WHERE user_id = %s AND article_pk = %s',
                    (user_id, article_pk),
                )
                return None
            await cur.execute(
                """
                INSERT INTO article_star (user_id, article_pk, starred_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, article_pk)
                    DO UPDATE SET starred_at = article_star.starred_at
                RETURNING starred_at
                """,
                (user_id, article_pk, utc_now_dt()),
            )
            row = await cur.fetchone()
        starred_at = row.get('starred_at') if row else None
        return starred_at.isoformat() if starred_at else None
