"""Highlights.

Anchored by ``quote`` + ``prefix`` + ``suffix`` — the W3C TextQuoteSelector
shape — never by character offset. Article bodies get re-fetched and
re-rendered, so an offset would silently point at different words after the
next sync; a quote plus its surroundings degrades to "not found" instead.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from ..utils import utc_now_dt

_COLUMNS = 'id, article_pk, quote, prefix, suffix, note, color, created_at'

#: Context kept on each side for disambiguation. Long enough to separate
#: repeated headings, short enough that a re-render nearby does not break it.
CONTEXT_LENGTH = 32


def _row(row: dict[str, Any]) -> dict[str, Any]:
    created = row.get('created_at')
    return {
        'id': row['id'],
        'article_id': row['article_pk'],
        'quote': row['quote'],
        'prefix': row['prefix'],
        'suffix': row['suffix'],
        'note': row['note'],
        'color': row['color'],
        'created_at': created.isoformat() if created else None,
    }


class AnnotationRepository:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def list_for_article(self, user_id: int, article_pk: int) -> list[dict[str, Any]]:
        """Every highlight one user has on one article, in creation order."""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS} FROM annotation
                WHERE user_id = %s AND article_pk = %s
                ORDER BY id
                """,
                (user_id, article_pk),
            )
            rows = await cur.fetchall()
        return [_row(row) for row in rows]

    async def create(
        self,
        *,
        user_id: int,
        article_pk: int,
        quote: str,
        prefix: str = '',
        suffix: str = '',
        note: str = '',
        color: str = 'default',
    ) -> dict[str, Any]:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO annotation
                    (user_id, article_pk, quote, prefix, suffix, note, color, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {_COLUMNS}
                """,
                (
                    user_id,
                    article_pk,
                    quote,
                    prefix[-CONTEXT_LENGTH:],
                    suffix[:CONTEXT_LENGTH],
                    note,
                    color,
                    utc_now_dt(),
                ),
            )
            row = await cur.fetchone()
        return _row(row)

    async def delete(self, annotation_id: int, user_id: int) -> bool:
        """Delete one highlight. The ``user_id`` predicate is the ownership check."""
        async with self._conn.cursor() as cur:
            await cur.execute(
                'DELETE FROM annotation WHERE id = %s AND user_id = %s',
                (annotation_id, user_id),
            )
            return cur.rowcount > 0
