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
from ..media_settings import get_media_settings
from ..storage import PostgresStorage
from . import SourceAdapter
from .bilibili import SOURCE as BILIBILI_SOURCE
from .bilibili import BilibiliClient, BilibiliResolver, BilibiliSource
from .ingest import MediaResolverRegistry, MediaSync
from .youtube import SOURCE as YOUTUBE_SOURCE
from .youtube import YoutubeResolver, YoutubeSource


class MediaSources:
    """Everything a media source needs for one run, built from ``media:settings``."""

    def __init__(self, *, storage: PostgresStorage, settings: Mapping[str, Any]) -> None:
        self.adapters: dict[str, SourceAdapter] = {}
        self.resolvers: dict[str, MediaResolver] = {}
        self._closers: list[Any] = []

        bilibili = self._client(
            BilibiliClient(
                cookie=str(settings.get('bilibili_cookie') or ''),
                proxy=str(settings.get('proxy_bilibili') or ''),
            )
        )
        self.adapters[BILIBILI_SOURCE] = MediaSync(storage=storage, source=BilibiliSource(bilibili))
        self.resolvers[BILIBILI_SOURCE] = BilibiliResolver(
            bilibili,
            use_cc_subtitle=bool(settings.get('bilibili_cc_subtitle')),
        )

        # yt-dlp 是子进程，没有客户端要管，只需要代理设置。
        youtube_proxy = str(settings.get('proxy_youtube') or '')
        self.adapters[YOUTUBE_SOURCE] = MediaSync(
            storage=storage,
            source=YoutubeSource(proxy=youtube_proxy),
        )
        self.resolvers[YOUTUBE_SOURCE] = YoutubeResolver(proxy=youtube_proxy)

    def _client(self, client: Any) -> Any:
        self._closers.append(client.aclose)
        return client

    def resolver_registry(self) -> MediaResolverRegistry:
        return MediaResolverRegistry(self.resolvers)

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
