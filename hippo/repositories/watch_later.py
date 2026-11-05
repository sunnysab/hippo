"""Watch Later: one ordered list per reader.

Read state and stars are unordered side tables; this one has an order the reader
arranges, so a row keeps both when it was added and where it sits. Visibility is
not the repository's business: the query layer decides which entries the caller
may still see and hands the new order here.

Every mutation locks the caller's ``users`` row first. Appending reads the
current maximum position, so two concurrent appends from the same reader would
otherwise pick the same one; the lock serializes the whole list per user without
touching a table the sync workers write.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg.rows import dict_row

from ..utils import utc_now_dt


class WatchLaterRepository:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def _lock_user(self, cur: psycopg.AsyncCursor, user_id: int) -> None:
        """Take the reader's row for the rest of the transaction."""
        await cur.execute('SELECT 1 FROM users WHERE id = %s FOR UPDATE', (user_id,))

    async def lock(self, *, user_id: int) -> None:
        """Take the reader's row before the caller reads the list.

        The query layer builds a new order from a snapshot of the entries; that
        snapshot is only valid while no other append or move runs. Locking inside
        the write would be too late, because the order would already have been
        computed from a list another transaction is about to change. Redundant
        re-locks in the same transaction cost nothing.
        """
        async with self._conn.cursor() as cur:
            await self._lock_user(cur, user_id)

    async def add(self, *, user_id: int, article_pk: int) -> str:
        """Append one article and report when it was first added.

        Idempotent: a second call keeps the first ``added_at`` *and* the first
        position, so re-adding an article never moves it up the list.
        """
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await self._lock_user(cur, user_id)
            await cur.execute(
                """
                INSERT INTO article_watch_later (user_id, article_pk, added_at, position)
                VALUES (
                    %s,
                    %s,
                    %s,
                    COALESCE(
                        (SELECT MAX(position) FROM article_watch_later WHERE user_id = %s),
                        0
                    ) + 1
                )
                ON CONFLICT (user_id, article_pk)
                    DO UPDATE SET added_at = article_watch_later.added_at
                RETURNING added_at
                """,
                (user_id, article_pk, utc_now_dt(), user_id),
            )
            row = await cur.fetchone()
        added_at = row.get('added_at') if row else None
        return added_at.isoformat() if added_at else ''

    async def remove(self, *, user_id: int, article_pk: int) -> None:
        """Take one article off the list.

        Idempotent, and it asks for no access: an entry stays removable after the
        reader lost sight of the article.
        """
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await self._lock_user(cur, user_id)
            await cur.execute(
                'DELETE FROM article_watch_later WHERE user_id = %s AND article_pk = %s',
                (user_id, article_pk),
            )

    async def entries(self, *, user_id: int) -> list[dict[str, Any]]:
        """Every stored entry of one reader, in list order."""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                'SELECT article_pk, added_at, position FROM article_watch_later'
                ' WHERE user_id = %s ORDER BY position ASC, article_pk ASC',
                (user_id,),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def set_positions(self, *, user_id: int, assignments: Sequence[tuple[int, int]]) -> None:
        """Write ``(article_pk, position)`` pairs for one reader.

        The caller owns the values; this only persists them. Rows that are not
        listed keep their position, which is what makes a move skip entries the
        reader can no longer see.
        """
        if not assignments:
            return
        async with self._conn.cursor() as cur:
            await self._lock_user(cur, user_id)
            await cur.executemany(
                'UPDATE article_watch_later SET position = %s WHERE user_id = %s AND article_pk = %s',
                [(position, user_id, article_pk) for article_pk, position in assignments],
            )

    async def added_at_for(self, *, user_id: int, article_pks: Sequence[int]) -> dict[int, str]:
        """``article_pk -> added_at`` for one reader, in a single query.

        Lets a page of articles report its badge without one lookup per row.
        Articles that are not on the list simply do not appear.
        """
        if not article_pks:
            return {}
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                'SELECT article_pk, added_at FROM article_watch_later WHERE user_id = %s AND article_pk = ANY(%s)',
                (user_id, list(article_pks)),
            )
            rows = await cur.fetchall()
        return {int(row['article_pk']): row['added_at'].isoformat() for row in rows}
