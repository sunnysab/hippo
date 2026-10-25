"""Topic classification queue: backfill, lease claim, and a guarded finish.

The shape is the outline queue's: SQL picks and orders the work, Python hashes
and calls the model, and ``finish`` refuses any write that does not own the
current lease. Nothing here decides what a topic means -- that lives in
``hippo/topics.py``.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


class TopicRepository:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def source(self, article_pk: int) -> dict[str, Any] | None:
        """The two fields the classifier sees, straight from the articles row."""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute('SELECT title, digest FROM articles WHERE id = %s', (article_pk,))
            return await cur.fetchone()

    async def get(self, article_pk: int) -> dict[str, Any] | None:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute('SELECT * FROM article_topic WHERE article_pk = %s', (article_pk,))
            return await cur.fetchone()

    async def backfill(self, *, since_ts: int, vocabulary_version: int, limit: int = 200) -> int:
        """Queue articles that have no classification row yet.

        ``NOT EXISTS`` is what makes this terminate: with only ``ON CONFLICT DO
        NOTHING`` the same first N rows would be selected every round, insert
        nothing, and the queue would never reach the rows behind them.
        """
        async with self._conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO article_topic (article_pk, state, vocabulary_version)
                   SELECT a.id, 'queued', %s
                   FROM articles a
                   WHERE a.publish_at >= %s
                     AND NOT EXISTS (SELECT 1 FROM article_topic t WHERE t.article_pk = a.id)
                   ORDER BY a.id
                   LIMIT %s
                   ON CONFLICT (article_pk) DO NOTHING""",
                (vocabulary_version, since_ts, limit),
            )
            return cur.rowcount

    async def requeue_stale(self, *, vocabulary_version: int, limit: int = 500) -> int:
        """Requeue rows classified under an older vocabulary or prompt.

        Batched on purpose: a whole-library requeue in one statement would hold
        locks and grow the WAL for as long as it runs.
        """
        async with self._conn.cursor() as cur:
            await cur.execute(
                """UPDATE article_topic
                   SET state = 'queued', topics = '[]'::jsonb, slugs = '{}', lease_token = NULL,
                       model = NULL, error_code = NULL, updated_at = clock_timestamp(),
                       vocabulary_version = %s
                   WHERE article_pk IN (
                       SELECT article_pk FROM article_topic
                       WHERE vocabulary_version <> %s
                       ORDER BY article_pk LIMIT %s)""",
                (vocabulary_version, vocabulary_version, limit),
            )
            return cur.rowcount

    async def claim(self) -> dict[str, Any] | None:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """WITH candidate AS (
                       SELECT article_pk FROM article_topic
                       WHERE state = 'queued'
                          OR (state = 'processing' AND updated_at < clock_timestamp() - INTERVAL '15 minutes')
                       ORDER BY updated_at, article_pk LIMIT 1 FOR UPDATE SKIP LOCKED
                   )
                   UPDATE article_topic AS t SET state = 'processing', lease_token = %s,
                       updated_at = clock_timestamp()
                   FROM candidate c WHERE t.article_pk = c.article_pk RETURNING t.*""",
                (uuid4(),),
            )
            return await cur.fetchone()

    async def finish(
        self,
        claim: dict,
        *,
        topics: list[dict[str, str]] | None = None,
        fingerprint: str | None = None,
        vocabulary_version: int | None = None,
        model: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Publish a result only while this caller still owns the lease.

        ``fingerprint`` and ``vocabulary_version`` move together with the topics:
        a row whose fingerprint did not advance would be picked up by the next
        backfill and classified again.
        """
        slugs = [topic['slug'] for topic in topics or []]
        async with self._conn.cursor() as cur:
            await cur.execute(
                """UPDATE article_topic
                   SET state = %s, topics = %s, slugs = %s, model = %s, error_code = %s,
                       fingerprint = COALESCE(%s, fingerprint),
                       vocabulary_version = COALESCE(%s, vocabulary_version),
                       lease_token = NULL, updated_at = clock_timestamp()
                   WHERE article_pk = %s AND state = 'processing' AND lease_token = %s""",
                (
                    'failed' if error else 'ready',
                    Jsonb(topics or []),
                    slugs,
                    model,
                    error,
                    fingerprint,
                    vocabulary_version,
                    claim['article_pk'],
                    claim['lease_token'],
                ),
            )
            return cur.rowcount > 0

    async def release(self, claim: dict) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                """UPDATE article_topic SET state = 'queued', lease_token = NULL, updated_at = clock_timestamp()
                   WHERE article_pk = %s AND state = 'processing' AND lease_token = %s""",
                (claim['article_pk'], claim['lease_token']),
            )
