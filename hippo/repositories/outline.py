"""Shared per-article outline cache and a PostgreSQL lease-backed work queue."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


class OutlineRepository:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def source(self, article_pk: int) -> dict[str, Any] | None:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT a.title, c.content_json, c.updated_at AS content_updated_at
                   FROM articles a LEFT JOIN article_content c ON c.article_pk = a.id WHERE a.id = %s""",
                (article_pk,),
            )
            return await cur.fetchone()

    async def get(self, article_pk: int) -> dict[str, Any] | None:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute('SELECT * FROM article_outline WHERE article_pk = %s', (article_pk,))
            return await cur.fetchone()

    async def enqueue(self, article_pk: int, fingerprint: str, *, force: bool = False) -> dict[str, Any]:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """INSERT INTO article_outline (article_pk, fingerprint, state)
                   VALUES (%s, %s, 'queued')
                   ON CONFLICT (article_pk) DO UPDATE
                   SET fingerprint = EXCLUDED.fingerprint, state = 'queued', sections = '[]'::jsonb,
                       lease_token = NULL, model = NULL, error_code = NULL, updated_at = clock_timestamp()
                   WHERE article_outline.fingerprint <> EXCLUDED.fingerprint
                      OR (article_outline.state NOT IN ('queued', 'processing')
                          AND (%s OR article_outline.state = 'failed'))""",
                (article_pk, fingerprint, force),
            )
        return await self.get(article_pk)

    async def claim(self) -> dict[str, Any] | None:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """WITH candidate AS (
                       SELECT article_pk FROM article_outline
                       WHERE state = 'queued'
                          OR (state = 'processing' AND updated_at < clock_timestamp() - INTERVAL '15 minutes')
                       ORDER BY updated_at, article_pk LIMIT 1 FOR UPDATE SKIP LOCKED
                   )
                   UPDATE article_outline AS o SET state = 'processing', lease_token = %s,
                       updated_at = clock_timestamp()
                   FROM candidate c WHERE o.article_pk = c.article_pk RETURNING o.*""",
                (uuid4(),),
            )
            return await cur.fetchone()

    async def finish(
        self, claim: dict, *, sections: list | None = None, model: str | None = None, error: str | None = None
    ):
        async with self._conn.cursor() as cur:
            await cur.execute(
                """UPDATE article_outline SET state = %s, sections = %s, model = %s, error_code = %s,
                       lease_token = NULL, updated_at = clock_timestamp()
                   WHERE article_pk = %s AND fingerprint = %s AND state = 'processing' AND lease_token = %s""",
                (
                    'failed' if error else 'ready',
                    Jsonb(sections or []),
                    model,
                    error,
                    claim['article_pk'],
                    claim['fingerprint'],
                    claim['lease_token'],
                ),
            )
            return cur.rowcount > 0

    async def release(self, claim: dict) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                """UPDATE article_outline SET state = 'queued', lease_token = NULL, updated_at = clock_timestamp()
                   WHERE article_pk = %s AND state = 'processing' AND lease_token = %s""",
                (claim['article_pk'], claim['lease_token']),
            )
