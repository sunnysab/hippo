"""Direct article references, identity lookup and shared queue bindings."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..wechat_links import WechatLink


class ArticleReferenceRepository:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def resolve(self, links: list[WechatLink]) -> dict[str, dict[str, Any]]:
        if not links:
            return {}
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT input.identity, a.id, a.title, a.link,
                       CASE WHEN jsonb_typeof(c.content_json) = 'array'
                            THEN jsonb_array_length(c.content_json) > 0 ELSE FALSE END AS ready
                  FROM jsonb_to_recordset(%s) AS input(identity text, biz text, sn text, slug text)
                  JOIN LATERAL (
                      SELECT id FROM articles
                       WHERE input.slug <> '' AND article_id = input.slug
                      UNION
                      SELECT id FROM articles
                       WHERE input.sn <> '' AND biz = input.biz AND raw_json::jsonb->>'sn' = input.sn
                      UNION
                      SELECT target_article_pk FROM article_reference
                       WHERE identity = input.identity AND target_article_pk IS NOT NULL
                  ) candidate ON TRUE
                  JOIN articles a ON a.id = candidate.id
                  JOIN accounts acc ON acc.biz = a.biz AND acc.source = 'weixin'
                  LEFT JOIN article_content c ON c.article_pk = a.id
                 ORDER BY a.id
                """,
                (Jsonb([asdict(link) for link in links]),),
            )
            rows = await cur.fetchall()
        return {row['identity']: dict(row) for row in rows}

    async def register(self, article_pk: int, links: list[WechatLink], targets: dict[str, dict]) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                'DELETE FROM article_reference WHERE source_article_pk = %s AND NOT (identity = ANY(%s))',
                (article_pk, [link.identity for link in links]),
            )
            unique = {link.identity: link for link in links}
            for identity in sorted(unique):
                link = unique[identity]
                target = targets.get(link.identity)
                await cur.execute(
                    """
                    INSERT INTO article_reference
                        (source_article_pk, source_updated_at, url, identity, target_article_pk)
                    SELECT article_pk, updated_at, %s, %s, %s
                      FROM article_content WHERE article_pk = %s
                    ON CONFLICT (source_article_pk, identity) DO UPDATE SET
                        url = EXCLUDED.url,
                        source_updated_at = EXCLUDED.source_updated_at,
                        target_article_pk = COALESCE(EXCLUDED.target_article_pk, article_reference.target_article_pk)
                    """,
                    (link.url, link.identity, target['id'] if target else None, article_pk),
                )

    async def enqueue(self, link: WechatLink, *, article_pk: int, retry: bool = False) -> int:
        """An explicit retry alone resets a failed job; normal opens are idempotent."""
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO article_queue (biz, sn, appmsg_id, long_link, payload, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (biz, sn) DO UPDATE SET
                    long_link = CASE WHEN article_queue.state IN ('failed', 'done')
                                     THEN EXCLUDED.long_link ELSE article_queue.long_link END,
                    state = CASE WHEN article_queue.state = 'done'
                                      OR (%s AND article_queue.state = 'failed')
                                 THEN 'pending' ELSE article_queue.state END,
                    attempts = CASE WHEN article_queue.state = 'done'
                                         OR (%s AND article_queue.state = 'failed')
                                    THEN 0 ELSE article_queue.attempts END,
                    updated_at = CASE WHEN article_queue.state = 'done'
                                           OR (%s AND article_queue.state = 'failed')
                                      THEN NOW() ELSE article_queue.updated_at END
                RETURNING id
                """,
                (
                    link.biz,
                    link.queue_sn,
                    link.mid or None,
                    link.url,
                    Jsonb({'source': 'article_reference'}),
                    retry,
                    retry,
                    retry,
                ),
            )
            queue_id = int((await cur.fetchone())[0])
            # Lock only this source: another reader may hold a different source row for the same identity.
            await cur.execute(
                'UPDATE article_reference SET queue_id = %s WHERE source_article_pk = %s AND identity = %s',
                (queue_id, article_pk, link.identity),
            )
        return queue_id

    async def states(self, links: list[WechatLink]) -> dict[str, dict[str, Any]]:
        if not links:
            return {}
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT input.identity, q.state
                  FROM jsonb_to_recordset(%s) AS input(identity text, biz text, queue_sn text)
                  JOIN article_queue q ON q.biz = input.biz AND q.sn = input.queue_sn
                """,
                (Jsonb([{'identity': link.identity, 'biz': link.biz, 'queue_sn': link.queue_sn} for link in links]),),
            )
            rows = await cur.fetchall()
        return {row['identity']: dict(row) for row in rows}

    async def complete(self, queue_id: int, article_pk: int) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                'UPDATE article_reference SET target_article_pk = %s WHERE queue_id = %s',
                (article_pk, queue_id),
            )

    async def ensure_publisher(
        self,
        *,
        biz: str,
        nickname: str,
        gh_id: str | None = None,
        alias: str | None = None,
        avatar_url: str | None = None,
    ) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO accounts (biz, nickname, source, gh_id, alias, round_head_img,
                                      backfill_state, created_at, updated_at)
                VALUES (%s, %s, 'weixin', %s, %s, %s, 'done', NOW(), NOW())
                ON CONFLICT (biz) DO NOTHING
                """,
                (biz, nickname, gh_id, alias, avatar_url),
            )
