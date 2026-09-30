"""Article image metadata and stored binaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from ..utils import utc_now_dt


@dataclass(frozen=True)
class ArticleImageTarget:
    article_pk: int
    image_id: int
    s3_key: str | None


class ImageRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    async def get_image_hash(self, image_id: int) -> dict[str, Any] | None:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT id, article_pk, hash_algo, content_hash
                FROM article_images
                WHERE id = %s
                """,
                (image_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def save_image_hash(
        self,
        *,
        image_id: int,
        hash_algo: str,
        content_hash: str,
    ) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE article_images
                SET hash_algo = %s,
                    content_hash = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (hash_algo, content_hash, utc_now_dt(), image_id),
            )
            if cur.rowcount == 0:
                raise LookupError(f'Image {image_id} not found')

    async def block_image_hash(
        self,
        *,
        hash_algo: str,
        content_hash: str,
        source_image_id: int | None,
    ) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO blocked_image_hashes (hash_algo, content_hash, source_image_id, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (hash_algo, content_hash) DO UPDATE SET
                    source_image_id = COALESCE(blocked_image_hashes.source_image_id, EXCLUDED.source_image_id)
                """,
                (hash_algo, content_hash, source_image_id, utc_now_dt()),
            )

    async def has_blocked_hashes(self) -> bool:
        async with self._conn.cursor() as cur:
            await cur.execute('SELECT 1 FROM blocked_image_hashes LIMIT 1')
            return await cur.fetchone() is not None

    async def get_article_images(self, article_pk: int) -> list[dict[str, Any]]:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT id, position, kind, content_type, hash_algo, content_hash
                FROM article_images
                WHERE article_pk = %s
                ORDER BY position ASC
                """,
                (article_pk,),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def list_blocked_image_ids(self, article_pk: int) -> set[int]:
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                SELECT i.id
                FROM article_images i
                JOIN blocked_image_hashes b
                  ON b.hash_algo = i.hash_algo
                 AND b.content_hash = i.content_hash
                WHERE i.article_pk = %s
                """,
                (article_pk,),
            )
            rows = await cur.fetchall()
        return {int(row[0]) for row in rows}

    async def get_article_image_target(self, biz: str, article_id: str, orig_url: str) -> ArticleImageTarget | None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                'SELECT id FROM articles WHERE biz = %s AND article_id = %s',
                (biz, article_id),
            )
            row = await cur.fetchone()
            if not row:
                return None
            article_pk = row[0]
            await cur.execute(
                'SELECT id, s3_key FROM article_images WHERE article_pk = %s AND orig_url = %s',
                (article_pk, orig_url),
            )
            image_row = await cur.fetchone()
            if not image_row:
                return None
            image_id, existing_key = image_row
        return ArticleImageTarget(article_pk=article_pk, image_id=image_id, s3_key=existing_key)

    async def update_article_image_metadata(
        self,
        *,
        article_pk: int,
        orig_url: str,
        content_type: str | None,
        s3_key: str,
    ) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE article_images
                SET content_type = %s,
                    s3_key = %s,
                    failed_at = NULL,
                    failed_reason = NULL,
                    updated_at = %s
                WHERE article_pk = %s AND orig_url = %s
                """,
                (
                    content_type,
                    s3_key,
                    utc_now_dt(),
                    article_pk,
                    orig_url,
                ),
            )

    async def mark_article_image_failed(
        self,
        biz: str,
        article_id: str,
        orig_url: str,
        reason: str,
        max_attempts: int = 3,
    ) -> None:
        trimmed = reason.strip()
        if len(trimmed) > 5000:
            trimmed = trimmed[:5000]
        async with self._conn.cursor() as cur:
            await cur.execute(
                'SELECT id FROM articles WHERE biz = %s AND article_id = %s',
                (biz, article_id),
            )
            row = await cur.fetchone()
            if not row:
                return
            article_pk = row[0]
            await cur.execute(
                """
                UPDATE article_images
                SET attempts = attempts + 1,
                    failed_reason = %s,
                    updated_at = %s,
                    -- attempts 到上限才写 failed_at（= 放弃重试）；没到就留在候选集里等下一轮
                    failed_at = CASE WHEN attempts + 1 >= %s THEN %s ELSE NULL END
                WHERE article_pk = %s AND orig_url = %s
                """,
                (
                    trimmed,
                    utc_now_dt(),
                    max_attempts,
                    utc_now_dt(),
                    article_pk,
                    orig_url,
                ),
            )

    async def count_backlog(self) -> dict[str, int]:
        """待下载 / 已放弃的图片计数（由 worker 定期写进 meta，避免 web 轮询扫大表）。"""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE (s3_key IS NULL OR s3_key = '') AND failed_at IS NULL) AS pending,
                    COUNT(*) FILTER (WHERE (s3_key IS NULL OR s3_key = '') AND failed_at IS NOT NULL) AS failed
                  FROM article_images
                """
            )
            row = await cur.fetchone() or {}
        return {'pending': int(row.get('pending') or 0), 'failed': int(row.get('failed') or 0)}

    async def list_s3_keys_for_account(self, biz: str) -> list[str]:
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                SELECT i.s3_key
                FROM article_images i
                JOIN articles a ON a.id = i.article_pk
                WHERE a.biz = %s AND i.s3_key IS NOT NULL AND i.s3_key <> ''
                """,
                (biz,),
            )
            return [row[0] for row in await cur.fetchall()]
