"""Discover Caixin metadata separately from paced, resumable body downloads."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from ..downloader import ArticleDownloader
from ..logger import get_logger
from ..media_settings import get_media_settings
from ..models import ArticleRecord
from ..storage import PostgresStorage
from ..sync_types import SyncStats
from . import SourceDeferred
from .caixin import SOURCE, CaixinSource
from .caixin_pacing import CaixinPaused

logger = get_logger(__name__)


@asynccontextmanager
async def caixin_article_access(storage: PostgresStorage, article_id: str):
    """Hold identity ownership through the body/raw commit, across channels and workers."""
    async with storage.transaction():
        cursor = await storage.conn.execute(
            'SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))',
            (f'caixin:article:{article_id}',),
        )
        if not (await cursor.fetchone())[0]:
            raise CaixinPaused(30)
        yield


async def ingest_caixin_body(
    *,
    storage: PostgresStorage,
    downloader: ArticleDownloader,
    article: ArticleRecord,
    article_pk: int,
    body: dict,
) -> int:
    """Initial downloads and explicit refetches archive the same representation."""
    async with storage.transaction():
        # ingest_body commits internally, so archive first to share that commit.
        await storage.documents.save(article_pk=article_pk, source=SOURCE, raw_html=body['content_html'], raw_json=body)
        pk = await downloader.ingest_body(
            article=article,
            html=body['content_html'],
            title=article.title,
            with_images=True,
        )
    return pk


class CaixinSync:
    def __init__(
        self,
        *,
        storage: PostgresStorage,
        source: CaixinSource,
        credentials: Callable[[str], Awaitable[tuple[str, ...]]],
        downloader: ArticleDownloader | None = None,
    ) -> None:
        self._storage = storage
        self._source = source
        self._credentials = credentials
        self._downloader = downloader

    async def sync_account(self, *, biz: str, source_key: str, pages: int = 1, offset: str = '') -> SyncStats:
        settings = await get_media_settings(self._storage)
        limit = int(settings.get('items_per_sync') or 5)
        cursor = offset or None
        stats = SyncStats()
        for _ in range(max(1, pages)):
            try:
                listing = await self._source.list_items(source_key=source_key, limit=limit, cursor=cursor)
            except SourceDeferred:
                if stats.listed:
                    break
                raise
            records = [
                ArticleRecord(
                    biz=biz,
                    article_id=item.article_id,
                    title=item.title,
                    link=item.link,
                    source_url=item.link,
                    author=item.author,
                    digest=item.digest,
                    cover=item.cover,
                    publish_at=item.publish_at,
                    raw=item.raw,
                )
                for item in listing.items
            ]
            async with self._storage.transaction():
                stats.enqueued += await self._storage.articles.save_articles(records)
            stats.listed += len(records)
            stats.next_offset, stats.is_end = listing.next_cursor, listing.is_end
            cursor = listing.next_cursor
            if listing.is_end or not cursor:
                break
        return stats

    async def drain(self, *, limit: int | None = None) -> SyncStats:
        if self._downloader is None:
            raise RuntimeError('Caixin body processing requires a downloader')
        take = min(5, max(0, limit if limit is not None else 5))
        if not take:
            return SyncStats()
        rows = await self._storage.articles.list_missing_bodies(source=SOURCE, limit=take)
        await self._storage.commit()
        stats = SyncStats()
        for row in rows:
            article_id, biz = str(row['article_id']), str(row['biz'])
            try:
                async with caixin_article_access(self._storage, article_id):
                    if await self._storage.articles.has_article_content(biz, article_id):
                        continue
                    cached = await self._storage.documents.find_by_identity(source=SOURCE, article_id=article_id)
                    if cached:
                        body = dict(cached.get('raw_json') or {})
                        body['content_html'] = cached['raw_html']
                    else:
                        secrets = await self._credentials(biz)
                        # Try another credential on a later, paced attempt, never in a request burst.
                        cookie = secrets[int(row.get('attempts') or 0) % len(secrets)]
                        source = self._source.with_credential(cookie)
                        body = await source.fetch_article(str(row['link']))
                    raw = row.get('raw_json') or {}
                    if isinstance(raw, str):
                        raw = json.loads(raw)
                    article = ArticleRecord(
                        biz=biz,
                        article_id=article_id,
                        title=str(row.get('title') or body.get('title') or ''),
                        link=str(row['link']),
                        source_url=row.get('source_url') or row['link'],
                        author=row.get('author') or body.get('author'),
                        digest=row.get('digest'),
                        cover=row.get('cover'),
                        publish_at=row.get('publish_at'),
                        raw=raw,
                    )
                    await ingest_caixin_body(
                        storage=self._storage,
                        downloader=self._downloader,
                        article=article,
                        article_pk=int(row['id']),
                        body=body,
                    )
                stats.ingested += 1
            except SourceDeferred:
                break
            except Exception as exc:
                await self._storage.rollback()
                async with self._storage.transaction():
                    await self._storage.download_attempts.increment_attempt(biz, article_id, type(exc).__name__)
                logger.warning('Caixin body %s failed: %s', article_id, type(exc).__name__)
                stats.failed += 1
                break
        return stats
