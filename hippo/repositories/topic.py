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

    async def backfill(
        self, *, since_ts: int, vocabulary_version: int, prompt_version: int, limit: int = 200
    ) -> int:
        """Queue articles that have no classification row yet.

        ``NOT EXISTS`` is what makes this terminate: with only ``ON CONFLICT DO
        NOTHING`` the same first N rows would be selected every round, insert
        nothing, and the queue would never reach the rows behind them.
        """
        async with self._conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO article_topic (article_pk, state, vocabulary_version, prompt_version)
                   SELECT a.id, 'queued', %s, %s
                   FROM articles a
                   WHERE a.publish_at >= %s
                     AND NOT EXISTS (SELECT 1 FROM article_topic t WHERE t.article_pk = a.id)
                   ORDER BY a.id
                   LIMIT %s
                   ON CONFLICT (article_pk) DO NOTHING""",
                (vocabulary_version, prompt_version, since_ts, limit),
            )
            return cur.rowcount

    async def requeue_stale(
        self, *, vocabulary_version: int, prompt_version: int, limit: int = 500
    ) -> int:
        """Requeue rows produced by an older vocabulary or an older prompt.

        Both versions are compared here. ``PROMPT_VERSION`` is also hashed into the
        fingerprint, but a fingerprint cannot be compared in SQL (it needs the
        article's title and digest), so the version needs a column of its own or a
        prompt change would silently requeue nothing.

        Batched on purpose: a whole-library requeue in one statement would hold
        locks and grow the WAL for as long as it runs.
        """
        async with self._conn.cursor() as cur:
            await cur.execute(
                """UPDATE article_topic
                   SET state = 'queued', topics = '[]'::jsonb, slugs = '{}', lease_token = NULL,
                       model = NULL, error_code = NULL, updated_at = clock_timestamp(),
                       vocabulary_version = %s, prompt_version = %s
                   WHERE article_pk IN (
                       SELECT article_pk FROM article_topic
                       WHERE vocabulary_version <> %s OR prompt_version <> %s
                       ORDER BY article_pk LIMIT %s)""",
                (vocabulary_version, prompt_version, vocabulary_version, prompt_version, limit),
            )
            return cur.rowcount

    async def topics_for(self, article_pks: list[int]) -> dict[int, list[dict[str, Any]]]:
        """The published topics of several articles, keyed by article.

        One query per page: a per-row lookup would turn a 20-row page into 20
        round trips for a field that is often empty.
        """
        if not article_pks:
            return {}
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT article_pk, topics FROM article_topic"
                " WHERE article_pk = ANY(%s) AND state = 'ready'",
                (list(article_pks),),
            )
            return {int(row['article_pk']): list(row['topics'] or []) for row in await cur.fetchall()}

    async def candidates(self, *, hours: int = 72, limit: int = 30) -> list[dict[str, Any]]:
        """Recent events the classifier may attach this article to.

        Ranked by how many distinct publishers covered them, not by raw report
        count: ten publishers reporting once is a story, one publisher posting ten
        times is a habit. The window and the limit are what keep the prompt flat.

        Crucially this does **not** require two publishers. Visibility does, but a
        candidate list filtered the same way is a closed loop: a new event has one
        publisher by definition, so it would never be offered, so no second article
        could ever join it, so it would never reach the threshold. The threshold
        decides what a reader may see; it must not decide what the model may join.
        """
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT e.id, e.title, COUNT(DISTINCT a.biz) AS sources
                   FROM article_event e
                   JOIN article_event_link l ON l.event_id = e.id
                   JOIN articles a ON a.id = l.article_pk
                   WHERE e.last_seen_at > clock_timestamp() - make_interval(hours => %s)
                   GROUP BY e.id, e.title
                   ORDER BY sources DESC, e.last_seen_at DESC
                   LIMIT %s""",
                (hours, limit),
            )
            return list(await cur.fetchall())

    async def public_event(self, event_id: int) -> dict[str, Any] | None:
        """One event, but only if at least two publishers reported it.

        The threshold lives here rather than in a view so every reader -- list,
        detail, feed -- gets the same answer, and a single-publisher guess never
        becomes a visible "story".
        """
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT e.id, e.title, e.first_seen_at, e.last_seen_at,
                          COUNT(DISTINCT a.biz) AS source_count,
                          COUNT(*) AS article_count
                   FROM article_event e
                   JOIN article_event_link l ON l.event_id = e.id
                   JOIN articles a ON a.id = l.article_pk
                   WHERE e.id = %s
                   GROUP BY e.id, e.title, e.first_seen_at, e.last_seen_at
                   HAVING COUNT(DISTINCT a.biz) >= 2""",
                (event_id,),
            )
            return await cur.fetchone()

    async def event_of(self, article_pk: int) -> dict[str, Any] | None:
        """The visible event this article belongs to, if any."""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT e.id, e.title FROM article_event_link l
                   JOIN article_event e ON e.id = l.event_id
                   WHERE l.article_pk = %s""",
                (article_pk,),
            )
            return await cur.fetchone()

    async def attach_event(self, article_pk: int, event_id: int, *, confidence: float | None = None) -> None:
        """Link an article to an event and refresh the event's window.

        Re-classifying an article may move it to a different event, which is why
        this is an upsert on ``article_pk`` rather than an insert.
        """
        async with self._conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO article_event_link (article_pk, event_id, confidence)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (article_pk) DO UPDATE
                   SET event_id = EXCLUDED.event_id, confidence = EXCLUDED.confidence""",
                (article_pk, event_id, confidence),
            )
            await cur.execute(
                'UPDATE article_event SET last_seen_at = clock_timestamp() WHERE id = %s', (event_id,)
            )

    async def create_event(self, title: str, article_pk: int) -> int:
        """Open a new event and put this article in it, in one step."""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute('INSERT INTO article_event (title) VALUES (%s) RETURNING id', (title,))
            event_id = (await cur.fetchone())['id']
            await cur.execute(
                'INSERT INTO article_event_link (article_pk, event_id) VALUES (%s, %s)'
                ' ON CONFLICT (article_pk) DO UPDATE SET event_id = EXCLUDED.event_id',
                (article_pk, event_id),
            )
            return int(event_id)

    async def detach_event(self, article_pk: int) -> None:
        """Drop a stale link when a re-classification says there is no story."""
        async with self._conn.cursor() as cur:
            await cur.execute('DELETE FROM article_event_link WHERE article_pk = %s', (article_pk,))

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
        prompt_version: int | None = None,
        model: str | None = None,
        error: str | None = None,
        event: dict[str, Any] | None = None,
        keep_event: bool = False,
    ) -> bool:
        """Publish a result only while this caller still owns the lease.

        ``fingerprint`` and ``vocabulary_version`` move together with the topics:
        a row whose fingerprint did not advance would be picked up by the next
        backfill and classified again.

        ``event`` is applied in this same statement's transaction: a topic row that
        published without its event, or an event link without its topics, would be
        a half-written classification nobody would ever repair.
        """
        slugs = [topic['slug'] for topic in topics or []]
        async with self._conn.cursor() as cur:
            await cur.execute(
                """UPDATE article_topic
                   SET state = %s, topics = %s, slugs = %s, model = %s, error_code = %s,
                       fingerprint = COALESCE(%s, fingerprint),
                       vocabulary_version = COALESCE(%s, vocabulary_version),
                       prompt_version = COALESCE(%s, prompt_version),
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
                    prompt_version,
                    claim['article_pk'],
                    claim['lease_token'],
                ),
            )
            published = cur.rowcount > 0

        # ``keep_event`` separates "the model said this is not a story" from "the
        # model was not asked". Only the first one may drop an existing link.
        if published and not error and not keep_event:
            await self._apply_event(claim['article_pk'], event)
        return published

    async def _apply_event(self, article_pk: int, event: dict[str, Any] | None) -> None:
        """Write the event decision that came back with the topics.

        No event also means "remove the old link": a re-classification that no
        longer sees a story behind this article must not leave it attached to the
        one it used to belong to.
        """
        if not event:
            await self.detach_event(article_pk)
            return
        if event['action'] == 'join':
            await self.attach_event(article_pk, int(event['id']))
        elif event['action'] == 'new':
            await self.create_event(str(event['title']), article_pk)
        else:  # pragma: no cover - the parser never produces anything else
            raise ValueError(f'Unknown event action: {event["action"]!r}')

    async def release(self, claim: dict) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                """UPDATE article_topic SET state = 'queued', lease_token = NULL, updated_at = clock_timestamp()
                   WHERE article_pk = %s AND state = 'processing' AND lease_token = %s""",
                (claim['article_pk'], claim['lease_token']),
            )
