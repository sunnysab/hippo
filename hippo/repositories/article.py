"""Article, content-document and download-attempt access."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from ..models import ArticleRecord
from ..utils import utc_now_dt

ARTICLE_CONTENT_PRESENT_SQL = """
(
    (c.content_markdown IS NOT NULL AND btrim(c.content_markdown) <> '')
    OR (c.content_json IS NOT NULL AND c.content_json::text NOT IN ('[]', 'null'))
)
"""


def _row_to_article(row: dict[str, Any]) -> ArticleRecord:
    data = dict(row)
    raw_json = data.pop('raw_json', None)
    data['raw'] = json.loads(raw_json) if raw_json else {}
    return ArticleRecord.model_validate(data)


class ArticleRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    async def _ensure_cover_image(
        self,
        cur: psycopg.Cursor,
        *,
        article_pk: int,
        cover_url: str | None,
        now: datetime,
    ) -> int | None:
        if not cover_url:
            return None
        await cur.execute(
            """
            SELECT id, kind
            FROM article_images
            WHERE article_pk = %s AND orig_url = %s
            LIMIT 1
            """,
            (article_pk, cover_url),
        )
        row = await cur.fetchone()
        if row:
            image_id, kind = row[0], row[1]
            if kind != 'cover':
                await cur.execute(
                    """
                    UPDATE article_images
                    SET kind = 'cover',
                        position = 0,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (now, image_id),
                )
            return int(image_id)
        await cur.execute(
            """
            SELECT id
            FROM article_images
            WHERE article_pk = %s AND kind = 'cover'
            ORDER BY id DESC
            LIMIT 1
            """,
            (article_pk,),
        )
        row = await cur.fetchone()
        if row:
            image_id = int(row[0])
            await cur.execute(
                """
                UPDATE article_images
                SET orig_url = %s,
                    position = 0,
                    content_type = NULL,
                    s3_key = NULL,
                    failed_at = NULL,
                    failed_reason = NULL,
                    updated_at = %s
                WHERE id = %s
                """,
                (cover_url, now, image_id),
            )
            return image_id
        await cur.execute(
            """
            INSERT INTO article_images
                (article_pk, position, kind, orig_url, content_type, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (article_pk, 0, 'cover', cover_url, None, now),
        )
        return int((await cur.fetchone())[0])

    def _normalize_cover_id(self, cover: str | int | None) -> int | None:
        if cover is None:
            return None
        if isinstance(cover, int):
            return cover
        if isinstance(cover, str) and cover.isdigit():
            return int(cover)
        return None

    @staticmethod
    async def _upsert_article_row(
        cur: psycopg.Cursor,
        *,
        biz: str,
        article_id: str,
        title: str,
        item_show_type: int | None,
        author: str | None,
        digest: str | None,
        link: str,
        source_url: str | None,
        publish_at: int | None,
        raw_json: str,
        now: datetime,
        return_inserted: bool = False,
    ) -> tuple[int, bool]:
        await cur.execute(
            """
            INSERT INTO articles
                (biz, article_id, title, item_show_type, author, digest, cover, link, source_url,
                 publish_at, raw_json, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s)
            ON CONFLICT (biz, article_id) DO UPDATE SET
                title=EXCLUDED.title,
                item_show_type=COALESCE(EXCLUDED.item_show_type, articles.item_show_type),
                author=EXCLUDED.author,
                digest=EXCLUDED.digest,
                link=EXCLUDED.link,
                source_url=EXCLUDED.source_url,
                publish_at=EXCLUDED.publish_at,
                raw_json=EXCLUDED.raw_json,
                updated_at=EXCLUDED.updated_at
            RETURNING id"""
            + (', (xmax = 0) AS inserted' if return_inserted else ''),
            (
                biz,
                article_id,
                title,
                item_show_type,
                author,
                digest,
                None,
                link,
                source_url,
                publish_at,
                raw_json,
                now,
                now,
            ),
        )
        row = await cur.fetchone()
        article_pk = int(row[0])
        is_inserted = bool(row[1]) if return_inserted else False
        return article_pk, is_inserted

    async def save_articles(self, articles: Iterable[ArticleRecord]) -> int:
        now = utc_now_dt()
        inserted_count = 0
        async with self._conn.cursor() as cur:
            for article in articles:
                article_pk, is_inserted = await self._upsert_article_row(
                    cur,
                    biz=article.biz,
                    article_id=article.article_id,
                    title=article.title,
                    item_show_type=article.item_show_type,
                    author=article.author,
                    digest=article.digest,
                    link=article.link,
                    source_url=article.source_url,
                    publish_at=article.publish_at,
                    raw_json=json.dumps(article.raw, ensure_ascii=False),
                    now=now,
                    return_inserted=True,
                )
                cover_id = self._normalize_cover_id(article.cover)
                if cover_id is None:
                    cover_id = await self._ensure_cover_image(
                        cur,
                        article_pk=article_pk,
                        cover_url=str(article.cover) if article.cover else None,
                        now=now,
                    )
                if cover_id is not None:
                    await cur.execute(
                        'UPDATE articles SET cover = %s, updated_at = %s WHERE id = %s',
                        (cover_id, now, article_pk),
                    )
                if is_inserted:
                    inserted_count += 1
        return inserted_count

    async def save_article_content(
        self,
        article: ArticleRecord,
        *,
        url_token: str | None,
        title: str,
        item_show_type: int | None,
        content_markdown: str,
        content_blocks: list[dict],
        cover_url: str | None,
        images: list[dict],
    ) -> int:
        """写入正文（markdown + blocks）；返回 ``articles.id``（写文档表要用）。"""
        now = utc_now_dt()
        normalized_cover: str | None = None
        async with self._conn.cursor() as cur:
            if cover_url is not None:
                cover_id = self._normalize_cover_id(cover_url)
                if cover_id is not None:
                    await cur.execute(
                        'SELECT orig_url FROM article_images WHERE id = %s',
                        (cover_id,),
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        normalized_cover = str(row[0]).strip()
                else:
                    normalized_cover = str(cover_url).strip()
            if normalized_cover:
                has_cover = any(
                    image.get('kind') == 'cover' and str(image.get('orig_url') or '') == normalized_cover
                    for image in images
                )
                if not has_cover:
                    images = [
                        {
                            'orig_url': normalized_cover,
                            'kind': 'cover',
                            'position': 0,
                            'content_type': None,
                            'data': None,
                        },
                        *images,
                    ]
            article_pk, _ = await self._upsert_article_row(
                cur,
                biz=article.biz,
                article_id=article.article_id,
                title=title,
                item_show_type=item_show_type,
                author=article.author,
                digest=article.digest,
                link=article.link,
                source_url=article.source_url,
                publish_at=article.publish_at,
                raw_json=json.dumps(article.raw, ensure_ascii=False),
                now=now,
            )

            await cur.execute('DELETE FROM article_images WHERE article_pk = %s', (article_pk,))
            image_id_map: dict[str, int] = {}
            seen_orig_urls: set[str] = set()
            cover_id: int | None = None
            for image in images:
                orig_url = image.get('orig_url')
                if orig_url:
                    orig_url = str(orig_url)
                    if orig_url in seen_orig_urls:
                        continue
                    seen_orig_urls.add(orig_url)
                await cur.execute(
                    """
                    INSERT INTO article_images
                        (article_pk, position, kind, orig_url, content_type, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        article_pk,
                        image.get('position', 0),
                        image.get('kind', 'inline'),
                        orig_url,
                        image.get('content_type'),
                        now,
                    ),
                )
                image_id = (await cur.fetchone())[0]
                if orig_url:
                    image_id_map[orig_url] = image_id
                if image.get('kind') == 'cover' and cover_id is None:
                    cover_id = int(image_id)

            updated_blocks: list[dict] = []
            for block in content_blocks:
                if block.get('type') == 'image':
                    orig_url = block.get('orig_url')
                    image_id = image_id_map.get(str(orig_url)) if orig_url else None
                    updated = dict(block)
                    if image_id is not None:
                        updated['image_id'] = image_id
                    updated_blocks.append(updated)
                else:
                    updated_blocks.append(block)

            await cur.execute(
                """
                INSERT INTO article_content
                    (article_pk, url_token, content_markdown, content_json, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (article_pk) DO UPDATE SET
                    url_token=EXCLUDED.url_token,
                    content_markdown=EXCLUDED.content_markdown,
                    content_json=EXCLUDED.content_json,
                    updated_at=EXCLUDED.updated_at
                """,
                (
                    article_pk,
                    url_token,
                    content_markdown,
                    Json(updated_blocks),
                    now,
                    now,
                ),
            )
            if cover_id is not None:
                await cur.execute(
                    'UPDATE articles SET cover = %s, updated_at = %s WHERE id = %s',
                    (cover_id, now, article_pk),
                )
            else:
                await cur.execute(
                    'UPDATE articles SET cover = NULL, updated_at = %s WHERE id = %s',
                    (now, article_pk),
                )
        return article_pk

    async def has_article_content(self, biz: str, article_id: str) -> bool:
        async with self._conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT 1
                FROM article_content c
                JOIN articles a ON a.id = c.article_pk
                WHERE a.biz = %s AND a.article_id = %s
                  AND {ARTICLE_CONTENT_PRESENT_SQL}
                LIMIT 1
                """,
                (biz, article_id),
            )
            return await cur.fetchone() is not None

    async def get_article_content_ids(self, biz: str, article_ids: Iterable[str]) -> set[str]:
        ids = [item for item in article_ids if item]
        if not ids:
            return set()
        async with self._conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT a.article_id
                FROM article_content c
                JOIN articles a ON a.id = c.article_pk
                WHERE a.biz = %s AND a.article_id = ANY(%s)
                  AND {ARTICLE_CONTENT_PRESENT_SQL}
                """,
                (biz, ids),
            )
            return {row[0] for row in await cur.fetchall()}

    async def list_articles(
        self,
        biz: str,
        *,
        limit: int | None = 10,
        since_timestamp: int | None = None,
        exclude_downloaded: bool = False,
    ) -> list[ArticleRecord]:
        query_parts = ['SELECT a.* FROM articles a']
        params: list = []

        if exclude_downloaded:
            query_parts.append('LEFT JOIN article_content c ON c.article_pk = a.id')

        query_parts.append('WHERE a.biz = %s')
        params.append(biz)

        if exclude_downloaded:
            query_parts.append(f'AND (c.id IS NULL OR NOT {ARTICLE_CONTENT_PRESENT_SQL})')

        if since_timestamp is not None:
            query_parts.append('AND (a.publish_at IS NULL OR a.publish_at >= %s)')
            params.append(since_timestamp)

        query_parts.append('ORDER BY a.publish_at DESC NULLS LAST, a.id DESC')

        if limit is not None:
            query_parts.append('LIMIT %s')
            params.append(limit)

        query = '\n'.join(query_parts)

        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()
        return [_row_to_article(row) for row in rows]

    async def get_existing_article_ids(self, biz: str, article_ids: Iterable[str]) -> set[str]:
        ids = [item for item in article_ids if item]
        if not ids:
            return set()
        existing: set[str] = set()
        chunk_size = 900
        async with self._conn.cursor() as cur:
            for i in range(0, len(ids), chunk_size):
                chunk = ids[i : i + chunk_size]
                await cur.execute(
                    'SELECT article_id FROM articles WHERE biz = %s AND article_id = ANY(%s)',
                    (biz, chunk),
                )
                existing.update(row[0] for row in await cur.fetchall())
        return existing


class ArticleDocumentRepository:
    """`article_document`：原始正文/响应，与线上正文表分开存。"""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    async def save(
        self,
        *,
        article_pk: int,
        source: str,
        url_token: str | None = None,
        raw_html: str | None = None,
        raw_json: Any | None = None,
    ) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO article_document
                    (article_pk, source, url_token, raw_html, raw_json, fetched_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW(), NOW(), NOW())
                ON CONFLICT (article_pk) DO UPDATE SET
                    source = EXCLUDED.source,
                    url_token = COALESCE(EXCLUDED.url_token, article_document.url_token),
                    raw_html = COALESCE(EXCLUDED.raw_html, article_document.raw_html),
                    raw_json = COALESCE(EXCLUDED.raw_json, article_document.raw_json),
                    fetched_at = EXCLUDED.fetched_at,
                    updated_at = NOW()
                """,
                (
                    article_pk,
                    source,
                    url_token,
                    raw_html,
                    Json(raw_json) if raw_json is not None else None,
                ),
            )

    async def get(self, article_pk: int) -> dict[str, Any] | None:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                'SELECT article_pk, source, url_token, raw_html, raw_json, fetched_at, updated_at '
                'FROM article_document WHERE article_pk = %s',
                (article_pk,),
            )
            return await cur.fetchone()


class DownloadAttemptRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    async def get_attempts_map(self, biz: str, article_ids: Iterable[str]) -> dict[str, int]:
        ids = [i for i in article_ids if i]
        if not ids:
            return {}
        result: dict[str, int] = {}
        async with self._conn.cursor() as cur:
            await cur.execute(
                'SELECT article_id, attempts FROM article_download_attempts WHERE biz = %s AND article_id = ANY(%s)',
                (biz, ids),
            )
            for row in await cur.fetchall():
                result[row[0]] = row[1]
        return result

    async def increment_attempt(self, biz: str, article_id: str, error: str | None = None) -> None:
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO article_download_attempts (biz, article_id, attempts, last_error, last_attempt_at, created_at)
                VALUES (%s, %s, 1, %s, %s, %s)
                ON CONFLICT (biz, article_id) DO UPDATE SET
                    attempts = article_download_attempts.attempts + 1,
                    last_error = EXCLUDED.last_error,
                    last_attempt_at = EXCLUDED.last_attempt_at
                """,
                (biz, article_id, error, now, now),
            )

    async def get_articles_within_limit(self, biz: str, article_ids: Iterable[str], *, max_attempts: int) -> set[str]:
        ids = [i for i in article_ids if i]
        if not ids:
            return set()
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                SELECT article_id FROM article_download_attempts
                WHERE biz = %s AND article_id = ANY(%s) AND attempts >= %s
                """,
                (biz, ids, max_attempts),
            )
            exceeded = {row[0] for row in await cur.fetchall()}
        return set(ids) - exceeded
