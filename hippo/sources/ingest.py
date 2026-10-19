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

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..downloader import parse_markdown_blocks
from ..logger import get_logger
from ..media.pipeline import MediaJobSkipped, MediaResolver, ResolvedMedia
from ..media_settings import get_media_settings
from ..models import ArticleRecord
from ..normalize_html import normalize_html
from ..storage import PostgresStorage
from ..sync_types import SyncStats
from .chain import run_with_fallback

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ChannelInfo:
    """What a subscription address resolved to, for the confirmation dialog."""

    biz: str
    nickname: str
    source_key: str
    avatar_url: str | None = None
    #: The source this turned out to be, when parsing can tell (a feed with an audio
    #: enclosure is a podcast, one without is a text feed). Empty means "the source the
    #: caller already picked".
    source: str = ''


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

    def __init__(
        self,
        *,
        storage: PostgresStorage,
        source: MediaSource,
        credentials: Callable[[str], Awaitable[tuple[str, ...]]] | None = None,
    ) -> None:
        self._storage = storage
        self._source = source
        #: 一次给定 biz，返回按顺序要试的凭证。列表是共享的、cookie 是个人的，
        #: 所以「用谁的」只能由这层决定；不传就只用来源自带的那一份。
        self._credentials = credentials

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
        cursor: str | None = offset or None

        async def list_page(candidate: MediaSource) -> tuple[MediaSource, Listing]:
            return candidate, await candidate.list_items(source_key=source_key, limit=limit, cursor=cursor)

        # 第一页决定用谁的凭证：列表接口才是 cookie 的用途，选定之后翻页沿用同一份。
        if self._credentials is None:
            source, listing = await list_page(self._source)
        else:
            source, listing = await run_with_fallback(
                self._source,
                await self._credentials(biz),
                list_page,
            )

        stats = SyncStats()
        max_pages = max(int(pages), 1)
        for page_number in range(max_pages):
            stats.listed += len(listing.items)
            # 去重交给数据库：articles 是 upsert，media_job 有 UNIQUE(article_pk)
            # 且入队只把 failed/skipped 拉回 pending，所以重复列举既不会重复转写，
            # 也不会把 enqueued 这个「成本闸门」计数算错。
            pks = await self._store(biz, listing.items)
            stats.enqueued += await self._enqueue(listing.items, pks)
            cursor = listing.next_cursor
            # After the last page of this run there is nothing to fetch next: that
            # page would be dropped whole, and a list call is not free.
            if listing.is_end or not cursor or page_number + 1 >= max_pages:
                break
            listing = await source.list_items(source_key=source_key, limit=limit, cursor=cursor)
        await self._storage.commit()
        await self._refresh_profile(biz=biz, source_key=source_key)
        logger.info('媒体列表 %s：%d 条，新入队 %d 条', source_key, stats.listed, stats.enqueued)
        return stats

    async def _refresh_profile(self, *, biz: str, source_key: str) -> None:
        """Fill a missing channel picture or repair a stored YouTube banner crop.

        A subscription made before a source could report them (or imported by hand)
        keeps the raw channel id as its nickname and has no avatar. One metadata call
        per sync, and only while something is missing: once both are there this costs
        a single row read, so a source that is never short of them never pays for it.

        Runs after the listing is committed: a metadata or profile write failure is
        logged and dropped, never allowed to roll the stored articles back.
        """
        fetcher = getattr(self._source, 'channel_info', None)
        if fetcher is None:
            # Only a source whose metadata is cheap and channel-shaped offers this
            # (YouTube); the others would need a second listing pass.
            return
        try:
            account = await self._storage.accounts.get_account(biz)
            fields: dict[str, str] = {}
            # Older YouTube extraction picked numbered banner crops from the avatar CDN.
            is_banner = self._source.source == 'youtube' and '-fcrop64=' in (account.round_head_img or '')
            if not account.round_head_img or is_banner:
                info = await fetcher(source_key)
                if info.avatar_url:
                    fields['round_head_img'] = info.avatar_url
                if info.nickname and account.nickname == source_key:
                    # Only the raw id is replaced: a name someone already has stays.
                    fields['nickname'] = info.nickname
            if fields:
                # The profile update gets its own transaction so a failure here
                # cannot abort the connection the committed listing rode in on.
                async with self._storage.transaction():
                    await self._storage.accounts.update_account_fields(biz, **fields)
                    if 'round_head_img' in fields:
                        # The avatar endpoint serves cached bytes before reading the account URL.
                        await self._storage.conn.execute('DELETE FROM avatar_images WHERE biz = %s', (biz,))
                logger.info('Refreshed the channel profile of %s: %s', biz, ', '.join(sorted(fields)))
        except LookupError:
            return
        except Exception as exc:  # a refresh must never fail the listing
            logger.warning('Channel metadata refresh for %s failed: %s', biz, exc)
            return

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

    def __init__(
        self,
        resolvers: Mapping[str, MediaResolver],
        credentials: Callable[[str, str], Awaitable[tuple[str, ...]]] | None = None,
    ) -> None:
        self._resolvers = dict(resolvers)
        #: (biz, source) → 按顺序要试的凭证。转写走和列表同一条链：共享的频道、
        #: 个人的 cookie，失败的判定也一样（只有「凭证被拒」才换下一份）。
        self._credentials = credentials

    @property
    def sources(self) -> list[str]:
        return sorted(self._resolvers)

    async def resolve(self, job: Mapping[str, Any]) -> ResolvedMedia:
        source = str(job.get('source') or '')
        resolver = self._resolvers.get(source)
        if resolver is None:
            known = ', '.join(self.sources) or '无'
            raise MediaJobSkipped(f'没有 {source!r} 来源的媒体解析器（已注册：{known}）')
        if self._credentials is None or not callable(getattr(resolver, 'with_credential', None)):
            return await resolver.resolve(job)
        secrets = await self._credentials(str(job.get('biz') or ''), source)
        return await run_with_fallback(resolver, secrets, lambda candidate: candidate.resolve(job))


__all__ = [
    'ChannelInfo',
    'Item',
    'Listing',
    'MediaResolverRegistry',
    'MediaSource',
    'MediaSync',
]
