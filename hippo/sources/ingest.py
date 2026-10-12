"""Turning a media source's items into articles and transcription jobs.

Every source that is not WeChat goes through here: Bilibili, YouTube and podcasts
differ in how they list items and where the audio lives, not in what happens to an
item afterwards. Keeping that part shared is what stops the third source from
becoming a second sync pipeline.

An item becomes an article row immediately (title, cover, publish time), with the
body left for the transcription queue — so a reader sees what was found long
before the transcript exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..downloader import parse_markdown_blocks
from ..logger import get_logger
from ..media.pipeline import MediaJobSkipped, MediaResolver, ResolvedMedia
from ..media_settings import get_media_settings
from ..models import ArticleRecord
from ..normalize_html import normalize_html
from ..storage import PostgresStorage
from ..weixin_worker import SyncStats

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ChannelInfo:
    """What a subscription address resolved to, for the confirmation dialog."""

    biz: str
    nickname: str
    source_key: str
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class Item:
    """One listed video or episode, before it becomes an article."""

    article_id: str
    title: str
    link: str
    #: What the resolver is handed later. Usually the page URL, not a media URL:
    #: the audio URL is short-lived (Bilibili signs it), so it is resolved at
    #: transcription time rather than at list time.
    media_url: str
    publish_at: int | None = None
    cover: str | None = None
    digest: str | None = None
    author: str | None = None
    duration_seconds: int | None = None
    #: Some feeds carry the article itself (a blog's Atom `<content>`, RSS
    #: `content:encoded`). When present it becomes the article body and there is no
    #: media to transcribe, so nothing is queued.
    content_html: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Listing:
    items: list[Item]
    next_cursor: str | None = None
    is_end: bool = True


class MediaSource(Protocol):
    #: Matches ``accounts.source``.
    source: str

    async def preview(self, ref: str) -> ChannelInfo: ...

    async def list_items(self, *, source_key: str, limit: int, cursor: str | None = None) -> Listing: ...


class MediaSync:
    """List a channel, store what was found, queue what needs transcribing."""

    def __init__(self, *, storage: PostgresStorage, source: MediaSource) -> None:
        self._storage = storage
        self._source = source

    async def sync_account(
        self,
        *,
        biz: str,
        source_key: str,
        pages: int = 1,
        offset: str = '',
    ) -> SyncStats:
        settings = await get_media_settings(self._storage)
        limit = int(settings.get('items_per_sync') or 5)

        stats = SyncStats()
        cursor: str | None = offset or None
        for _ in range(max(int(pages), 1)):
            listing = await self._source.list_items(source_key=source_key, limit=limit, cursor=cursor)
            stats.listed += len(listing.items)
            # 去重交给数据库：articles 是 upsert，media_job 有 UNIQUE(article_pk)
            # 且入队只把 failed/skipped 拉回 pending，所以重复列举既不会重复转写，
            # 也不会把 enqueued 这个「成本闸门」计数算错。
            pks = await self._store(biz, listing.items)
            stats.enqueued += await self._enqueue(listing.items, pks)
            cursor = listing.next_cursor
            if listing.is_end or not cursor:
                break
        await self._storage.commit()
        logger.info('媒体列表 %s：%d 条，新入队 %d 条', source_key, stats.listed, stats.enqueued)
        return stats

    async def _store(self, biz: str, items: list[Item]) -> dict[str, int]:
        """Upsert the article rows and return ``article_id → article_pk``."""
        if not items:
            return {}
        records = [
            ArticleRecord(
                biz=biz,
                article_id=item.article_id,
                title=item.title,
                item_show_type=None,
                author=item.author,
                digest=item.digest,
                cover=item.cover,
                link=item.link,
                source_url=item.link,
                publish_at=item.publish_at,
                duration_seconds=item.duration_seconds,
                raw=item.raw,
            )
            for item in items
        ]
        await self._storage.articles.save_articles(records)
        pks = await self._article_pks(biz, [item.article_id for item in items])
        await self._store_bodies(pks, items, records)
        return pks

    async def _store_bodies(
        self,
        pks: Mapping[str, int],
        items: list[Item],
        records: list[ArticleRecord],
    ) -> None:
        """Write the body of a feed that carries one, the way a fetched article is.

        Reuses the reader-facing pipeline: clean the HTML, keep the text as markdown
        (search and export read that) and the blocks the reader renders.
        """
        by_article_id = {record.article_id: record for record in records}
        for item in items:
            if not item.content_html or item.article_id not in pks:
                continue
            article = by_article_id[item.article_id]
            markdown = normalize_html(item.content_html, 'markdown')
            _, _, blocks, _ = parse_markdown_blocks(markdown)
            await self._storage.articles.save_article_content(
                article,
                url_token=None,
                title=item.title,
                item_show_type=None,
                content_markdown=markdown,
                content_blocks=blocks,
                cover_url=item.cover,
                images=[],
            )

    async def _article_pks(self, biz: str, article_ids: list[str]) -> dict[str, int]:
        if not article_ids:
            return {}
        rows = await self._storage.conn.execute(
            'SELECT article_id, id FROM articles WHERE biz = %s AND article_id = ANY(%s)',
            (biz, article_ids),
        )
        return {str(article_id): int(pk) for article_id, pk in await rows.fetchall()}

    async def _enqueue(self, items: list[Item], pks: Mapping[str, int]) -> int:
        payload = [
            {
                'article_pk': pks[item.article_id],
                'source': self._source.source,
                'media_url': item.media_url,
                # 列表阶段已经知道的东西（转录稿地址、视频 id…）跟着任务走，
                # 解析阶段就不必把列表接口再拉一遍。
                'payload': item.raw,
            }
            for item in items
            # 没有音频的条目（博客 feed 的自带正文）不需要转写任务。
            if item.article_id in pks and item.media_url
        ]
        return await self._storage.media_jobs.enqueue_many(payload)


class MediaResolverRegistry:
    """Route a queued job to the resolver that knows its source.

    The worker has no idea which source it is draining; the job does.
    """

    def __init__(self, resolvers: Mapping[str, MediaResolver]) -> None:
        self._resolvers = dict(resolvers)

    @property
    def sources(self) -> list[str]:
        return sorted(self._resolvers)

    async def resolve(self, job: Mapping[str, Any]) -> ResolvedMedia:
        source = str(job.get('source') or '')
        resolver = self._resolvers.get(source)
        if resolver is None:
            known = ', '.join(self.sources) or '无'
            raise MediaJobSkipped(f'没有 {source!r} 来源的媒体解析器（已注册：{known}）')
        return await resolver.resolve(job)


__all__ = [
    'ChannelInfo',
    'Item',
    'Listing',
    'MediaResolverRegistry',
    'MediaSource',
    'MediaSync',
]
