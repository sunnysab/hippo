"""Constructing the media sources from the instance settings.

Both halves of a media source come from the same client: the adapter that lists a
channel's items, and the resolver that turns one queued item into something the
transcription pipeline can fetch. Building them together is what keeps the HTTP
client (and its cookie) shared instead of opened twice.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..media.pipeline import MediaResolver
from ..media_settings import get_media_settings, resolve_proxy
from ..storage import PostgresStorage
from . import SourceAdapter, UnsupportedSource
from .bilibili import SOURCE as BILIBILI_SOURCE
from .bilibili import BilibiliClient, BilibiliResolver, BilibiliSource
from .ingest import ChannelInfo, MediaResolverRegistry, MediaSource, MediaSync
from .podcast import SOURCE as PODCAST_SOURCE
from .podcast import PodcastClient, PodcastResolver, PodcastSource
from .youtube import SOURCE as YOUTUBE_SOURCE
from .youtube import YoutubeResolver, YoutubeSource


class MediaSources:
    """Everything a media source needs for one run, built from ``media:settings``."""

    def __init__(
        self,
        *,
        storage: PostgresStorage,
        settings: Mapping[str, Any],
        bilibili_cookie: str | None = None,
        youtube_cookies: str | None = None,
    ) -> None:
        self.adapters: dict[str, SourceAdapter] = {}
        self.resolvers: dict[str, MediaResolver] = {}
        #: 原始来源对象，供「粘 URL 订阅」时的预览用。
        self.sources: dict[str, MediaSource] = {}
        self._closers: list[Any] = []

        cookie = bilibili_cookie if bilibili_cookie is not None else str(settings.get('bilibili_cookie') or '')
        bilibili = self._client(
            BilibiliClient(
                cookie=cookie,
                proxy=resolve_proxy(settings, BILIBILI_SOURCE),
            )
        )
        bilibili_source = BilibiliSource(bilibili)
        self.sources[BILIBILI_SOURCE] = bilibili_source
        self.adapters[BILIBILI_SOURCE] = MediaSync(storage=storage, source=bilibili_source)
        self.resolvers[BILIBILI_SOURCE] = BilibiliResolver(
            bilibili,
            use_cc_subtitle=bool(settings.get('bilibili_cc_subtitle')),
        )

        podcast_proxy = resolve_proxy(settings, PODCAST_SOURCE)
        podcast = self._client(PodcastClient(proxy=podcast_proxy))
        podcast_source = PodcastSource(podcast)
        self.sources[PODCAST_SOURCE] = podcast_source
        self.adapters[PODCAST_SOURCE] = MediaSync(storage=storage, source=podcast_source)
        self.resolvers[PODCAST_SOURCE] = PodcastResolver(podcast, proxy=podcast_proxy)

        # yt-dlp 是子进程，没有客户端要管，只需要代理设置与 cookie（登录态是躲开
        # 「confirm you're not a bot」的唯一办法）。
        youtube_proxy = resolve_proxy(settings, YOUTUBE_SOURCE)
        cookies = (
            youtube_cookies if youtube_cookies is not None else str(settings.get('youtube_cookies') or '')
        )
        youtube_source = YoutubeSource(proxy=youtube_proxy, cookies=cookies)
        self.sources[YOUTUBE_SOURCE] = youtube_source
        self.adapters[YOUTUBE_SOURCE] = MediaSync(storage=storage, source=youtube_source)
        self.resolvers[YOUTUBE_SOURCE] = YoutubeResolver(proxy=youtube_proxy, cookies=cookies)

    def _client(self, client: Any) -> Any:
        self._closers.append(client.aclose)
        return client

    def resolver_registry(self) -> MediaResolverRegistry:
        return MediaResolverRegistry(self.resolvers)

    async def preview(self, source: str, url: str) -> ChannelInfo:
        """Resolve a pasted URL to a channel, for the confirmation dialog."""
        origin = self.sources.get(source)
        if origin is None:
            known = ', '.join(sorted(self.sources)) or '无'
            raise UnsupportedSource(f'还不支持 {source!r} 来源（已支持：{known}）')
        return await origin.preview(url)

    async def aclose(self) -> None:
        for close in self._closers:
            await close()
        self._closers.clear()


async def build_media_sources(
    *,
    storage: PostgresStorage,
    settings: Mapping[str, Any] | None = None,
) -> MediaSources:
    resolved = settings if settings is not None else await get_media_settings(storage)
    return MediaSources(storage=storage, settings=resolved)


__all__ = ['MediaSources', 'build_media_sources']
