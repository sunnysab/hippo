"""Media transcription jobs, archived audio and per-user source credentials.

``media_job`` uses the same state machine as ``article_queue`` (pending →
processing → done/failed, with attempts and a retry ceiling) because it is the
same kind of work: a queue an operator can watch, retry and reason about.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

#: Same ceiling as ``article_queue``: once the retries are used up the row turns
#: failed instead of being handed out again forever.
DEFAULT_MAX_ATTEMPTS = 3

#: A worker that dies mid-job leaves rows in ``processing``; after this long they
#: are handed back.
STALE_AFTER_SECONDS = 900

_TERMINAL_STATES = ('done', 'failed', 'skipped')


class MediaJobRepository:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def enqueue_many(self, items: Iterable[dict[str, Any]], *, force: bool = False) -> int:
        """Queue one job per article, keeping at most one row per ``article_pk``.

        Without ``force`` only failed/skipped rows are pulled back to pending: a
        finished transcript is not redone, and a running job is not restarted.
        ``force`` is what the reader's "转写这篇" button uses.
        """
        rows = [
            (int(item['article_pk']), str(item['source']), str(item['media_url']), Json(item.get('payload') or {}))
            for item in items
        ]
        if not rows:
            return 0
        async with self._conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO media_job (article_pk, source, media_url, payload, created_at, updated_at)
                VALUES (%s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (article_pk) DO UPDATE
                   SET media_url = EXCLUDED.media_url,
                       source = EXCLUDED.source,
                       payload = EXCLUDED.payload,
                       state = 'pending',
                       stage = NULL,
                       progress = 0,
                       last_error = NULL,
                       updated_at = NOW()
                 WHERE %s OR media_job.state IN ('failed', 'skipped')
                """,
                [(*row, force) for row in rows],
            )
            return cur.rowcount

    async def take_pending(self, limit: int) -> list[dict[str, Any]]:
        """Claim a batch atomically, so no lock is held across a network request."""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE media_job
                   SET state = 'processing', updated_at = NOW()
                 WHERE id IN (
                       SELECT id FROM media_job WHERE state = 'pending' ORDER BY id LIMIT %s
                 )
                RETURNING id, article_pk, source, media_url, payload, attempts,
                          -- 转写要按频道找订阅者的凭证，biz 只能从文章行上取。
                          (SELECT biz FROM articles WHERE id = media_job.article_pk) AS biz
                """,
                (max(int(limit), 1),),
            )
            return list(await cur.fetchall())

    async def requeue_stale(self, older_than_secs: int = STALE_AFTER_SECONDS) -> int:
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE media_job
                   SET state = 'pending', updated_at = NOW(),
                       last_error = COALESCE(last_error, 'worker 中断，已重排')
                 WHERE state = 'processing'
                   AND updated_at < NOW() - make_interval(secs => %s)
                """,
                (older_than_secs,),
            )
            return cur.rowcount

    async def requeue(self, job_ids: Iterable[int], *, error: str) -> int:
        """Infrastructure failure: back to pending **without** spending an attempt.

        Only ``processing`` rows move, so a batch that already finished is not
        pulled back in.
        """
        ids = [int(job_id) for job_id in job_ids]
        if not ids:
            return 0
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE media_job
                   SET state = 'pending', last_error = %s, updated_at = NOW()
                 WHERE id = ANY(%s) AND state = 'processing'
                """,
                (error[:1000], ids),
            )
            return cur.rowcount

    async def update_progress(self, job_id: int, *, stage: str, progress: int) -> None:
        """Keep the reader's "转写中" line moving."""
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE media_job
                   SET stage = %s, progress = %s, updated_at = NOW()
                 WHERE id = %s AND state = 'processing'
                """,
                (stage, min(max(int(progress), 0), 100), int(job_id)),
            )

    async def mark_done(self, job_id: int) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE media_job
                   SET state = 'done', stage = 'done', progress = 100, last_error = NULL,
                       updated_at = NOW()
                 WHERE id = %s
                """,
                (int(job_id),),
            )

    async def mark_failed(
        self,
        job_id: int,
        *,
        error: str,
        retryable: bool = True,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> str:
        """Record a failure and return the state the row ended up in."""
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE media_job
                   SET attempts = attempts + 1,
                       last_error = %s,
                       state = CASE WHEN %s AND attempts + 1 < %s THEN 'pending' ELSE 'failed' END,
                       updated_at = NOW()
                 WHERE id = %s
                RETURNING state
                """,
                (error[:1000], retryable, max(int(max_attempts), 1), int(job_id)),
            )
            row = await cur.fetchone()
        return str(row[0]) if row else 'failed'

    async def mark_skipped(self, job_id: int, *, reason: str) -> None:
        """Nothing to transcribe, or not worth transcribing (e.g. too long)."""
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE media_job
                   SET state = 'skipped', stage = 'skipped', last_error = %s, updated_at = NOW()
                 WHERE id = %s
                """,
                (reason[:1000], int(job_id)),
            )

    async def stats(self) -> dict[str, int]:
        """Counts with all four keys present, for the console."""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute('SELECT state, COUNT(*) AS n FROM media_job GROUP BY state')
            counts = {str(row['state']): int(row['n']) for row in await cur.fetchall()}
        return {state: counts.get(state, 0) for state in ('pending', 'processing', 'failed', 'done')}

    async def stats_by_source(self) -> dict[str, dict[str, int]]:
        """The same counts per source, for a per-source backlog on the source page.

        Every source is present with all four keys, so a card can render a zero
        backlog without checking whether the key exists.
        """
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute('SELECT source, state, COUNT(*) AS n FROM media_job GROUP BY source, state')
            rows = await cur.fetchall()
        per_source: dict[str, dict[str, int]] = {}
        for row in rows:
            bucket = per_source.setdefault(str(row['source']), {})
            bucket[str(row['state'])] = int(row['n'])
        return {
            source: {state: counts.get(state, 0) for state in ('pending', 'processing', 'failed', 'done')}
            for source, counts in per_source.items()
        }

    async def get(self, article_pk: int) -> dict[str, Any] | None:
        """The job for one article, so a reader action can re-queue it as it was."""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                'SELECT id, article_pk, source, media_url, payload, state, attempts, last_error'
                ' FROM media_job WHERE article_pk = %s',
                (int(article_pk),),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def states_for_articles(self, article_pks: Iterable[int]) -> dict[int, str]:
        """State per article, so a detail response can say "转写中"."""
        ids = [int(pk) for pk in article_pks]
        if not ids:
            return {}
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                'SELECT article_pk, state FROM media_job WHERE article_pk = ANY(%s)',
                (ids,),
            )
            return {int(row['article_pk']): str(row['state']) for row in await cur.fetchall()}

    async def list_failed(self, limit: int = 5) -> list[dict[str, Any]]:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT m.article_pk, m.source, m.media_url, m.attempts, m.last_error, m.updated_at,
                       a.title
                  FROM media_job m
                  LEFT JOIN articles a ON a.id = m.article_pk
                 WHERE m.state = 'failed'
                 ORDER BY m.updated_at DESC
                 LIMIT %s
                """,
                (max(int(limit), 1),),
            )
            rows = await cur.fetchall()
        return [
            {
                'article_pk': int(row['article_pk']),
                'title': row['title'],
                'source': row['source'],
                'media_url': row['media_url'],
                'attempts': int(row['attempts']),
                'last_error': row['last_error'],
                'updated_at': row['updated_at'].isoformat() if row['updated_at'] else None,
            }
            for row in rows
        ]


class ArticleMediaRepository:
    """The archived audio file for one article.

    Deliberately not ``article_images``: the image backfill loop scans for rows
    with an empty ``s3_key``, so an audio row there would be downloaded as if it
    were a picture.
    """

    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def save(
        self,
        *,
        article_pk: int,
        kind: str = 'audio',
        orig_url: str | None,
        content_type: str | None,
        size_bytes: int | None,
        duration_seconds: int | None,
        s3_key: str,
    ) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO article_media (
                    article_pk, kind, orig_url, content_type, size_bytes, duration_seconds,
                    s3_key, state, error, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'stored', NULL, NOW(), NOW())
                ON CONFLICT (article_pk, kind) DO UPDATE
                   SET orig_url = EXCLUDED.orig_url,
                       content_type = EXCLUDED.content_type,
                       size_bytes = EXCLUDED.size_bytes,
                       duration_seconds = EXCLUDED.duration_seconds,
                       s3_key = EXCLUDED.s3_key,
                       state = 'stored',
                       error = NULL,
                       updated_at = NOW()
                """,
                (
                    int(article_pk),
                    kind,
                    orig_url,
                    content_type,
                    size_bytes,
                    duration_seconds,
                    s3_key,
                ),
            )

    async def mark_failed(self, article_pk: int, *, kind: str = 'audio', error: str) -> None:
        """A failed archive must not fail the transcription it was made for."""
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO article_media (article_pk, kind, state, error, created_at, updated_at)
                VALUES (%s, %s, 'failed', %s, NOW(), NOW())
                ON CONFLICT (article_pk, kind) DO UPDATE
                   SET state = 'failed', error = EXCLUDED.error, updated_at = NOW()
                """,
                (int(article_pk), kind, error[:1000]),
            )

    async def get(self, article_pk: int, *, kind: str = 'audio') -> dict[str, Any] | None:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT article_pk, kind, orig_url, content_type, size_bytes, duration_seconds,
                       s3_key, state, error
                  FROM article_media
                 WHERE article_pk = %s AND kind = %s
                """,
                (int(article_pk), kind),
            )
            row = await cur.fetchone()
        return dict(row) if row else None


class UserCredentialRepository:
    """Per-user source credentials (the Bilibili cookie, for now).

    The API never returns the secret: only whether one is configured.
    """

    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def get(self, user_id: int, source: str) -> str | None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                'SELECT secret FROM user_credential WHERE user_id = %s AND source = %s',
                (int(user_id), source),
            )
            row = await cur.fetchone()
        return str(row[0]) if row and row[0] else None

    async def set(self, user_id: int, source: str, secret: str) -> None:
        """An empty secret clears the credential."""
        text = (secret or '').strip()
        if not text:
            await self.delete(user_id, source)
            return
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO user_credential (user_id, source, secret, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id, source) DO UPDATE
                   SET secret = EXCLUDED.secret, updated_at = NOW()
                """,
                (int(user_id), source, text),
            )

    async def delete(self, user_id: int, source: str) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                'DELETE FROM user_credential WHERE user_id = %s AND source = %s',
                (int(user_id), source),
            )

    async def for_biz(self, biz: str, source: str) -> list[str]:
        """Credentials of the users who follow ``biz``, most recently set first.

        A media account is shared, so "whose cookie" has no single answer; the
        callers' own are tried before the instance's (see ``sources.chain``).
        Only distinct secrets come back: two users pasting the same cookie would
        otherwise double the requests for no extra chance of success.
        """
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT c.secret
                  FROM user_credential c
                  JOIN subscription s ON s.user_id = c.user_id
                 WHERE s.biz = %s AND c.source = %s AND c.secret <> ''
                 ORDER BY c.updated_at DESC
                """,
                (biz, source),
            )
            rows = await cur.fetchall()
        seen: set[str] = set()
        secrets: list[str] = []
        for row in rows:
            secret = str(row['secret'])
            if secret in seen:
                continue
            seen.add(secret)
            secrets.append(secret)
        return secrets

    async def configured(self, user_id: int) -> dict[str, bool]:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT source, secret <> '' AS present
                  FROM user_credential
                 WHERE user_id = %s
                """,
                (int(user_id),),
            )
            return {str(row['source']): bool(row['present']) for row in await cur.fetchall()}


__all__ = [
    'DEFAULT_MAX_ATTEMPTS',
    'STALE_AFTER_SECONDS',
    'ArticleMediaRepository',
    'MediaJobRepository',
    'UserCredentialRepository',
]
