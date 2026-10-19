"""Service container for wiring core dependencies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .downloader import ArticleDownloader
from .file_storage import S3FileStorage
from .http import MPClient
from .image_store import ArticleImageService
from .media_settings import PROXY_ALIASES, PROXY_SOURCES, resolve_proxy
from .sources import SourceAdapter
from .sources.registry import MediaSources
from .sources.weixin import WeixinSourceAdapter
from .storage import PostgresStorage
from .weixin_source import WeixinSource
from .weixin_worker import WeixinArticleSync


@dataclass(slots=True)
class AppContainer:
    storage: PostgresStorage
    client: MPClient
    image_service: ArticleImageService | None
    downloader: ArticleDownloader | None
    # weixin-rs 数据源：列表入队 + 正文落库（替代微信读书来源）
    weixin_source: WeixinSource | None = None
    weixin_sync: WeixinArticleSync | None = None
    #: 按 ``accounts.source`` 选适配器。新增来源就是往这里加一项，不改同步流程。
    adapters: dict[str, SourceAdapter] = field(default_factory=dict)
    #: 媒体来源的 HTTP 客户端（B站等），随容器一起关闭。
    media_sources: MediaSources | None = None

    async def __aenter__(self) -> AppContainer:
        await self.client.__aenter__()
        if self.media_sources:
            pass
        if self.weixin_source:
            await self.weixin_source.__aenter__()
        if self.downloader:
            await self.downloader.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        if self.media_sources:
            await self.media_sources.aclose()
        if self.downloader:
            await self.downloader.__aexit__(exc_type, exc, tb)
        if self.weixin_source:
            await self.weixin_source.__aexit__(exc_type, exc, tb)
        await self.client.__aexit__(exc_type, exc, tb)


def build_sync_container(
    *,
    storage: PostgresStorage,
    enable_download: bool,
    enable_images: bool,
    media_settings: Mapping[str, Any] | None = None,
) -> AppContainer:
    # Article bodies, avatars and images are all WeChat traffic, so the client
    # uses the proxy configured for that source instead of one instance-wide exit.
    client = MPClient(proxy=_source_proxy(media_settings, 'weixin'))
    weixin_source = WeixinSource()
    image_service: ArticleImageService | None = None
    if enable_images:
        image_service = ArticleImageService(
            image_repo=storage.images,
            file_storage=S3FileStorage(),
            transaction=storage.transaction,
        )
    # 正文落库必须走 downloader（markdown/blocks/图片链路），所以不论
    # enable_download 与否都建；它只控制要不要额外跑图片下载 worker。
    downloader = ArticleDownloader(
        client=client,
        storage=storage,
        image_store=image_service,
        enable_image_worker=bool(enable_images and enable_download),
    )
    weixin_sync = WeixinArticleSync(
        storage=storage,
        source=weixin_source,
        downloader=downloader,
    )
    media_sources = MediaSources(storage=storage, settings=media_settings or {})
    adapters: dict[str, SourceAdapter] = {'weixin': WeixinSourceAdapter(weixin_sync)}
    if media_settings and not media_settings.get('enabled', True):
        # 总开关关掉时连适配器都不注册：订阅里的媒体账号会被报成「没有适配器」。
        media_sources = None
    elif media_sources:
        adapters.update(media_sources.adapters)
    return AppContainer(
        storage=storage,
        client=client,
        image_service=image_service,
        downloader=downloader,
        weixin_source=weixin_source,
        weixin_sync=weixin_sync,
        adapters=adapters,
        media_sources=media_sources,
    )


def build_downloader_container(
    *,
    storage: PostgresStorage,
    enable_images: bool,
    article_worker: str | None = None,
    article_worker_proxy: str | None = None,
    article_max_connections: int | None = None,
    image_workers: int | None = None,
    enable_image_worker: bool = True,
    proxy: str | None = None,
) -> AppContainer:
    client_kwargs: dict[str, Any] = {}
    if article_worker is not None:
        client_kwargs['article_worker'] = article_worker
    if article_worker_proxy is not None:
        client_kwargs['article_worker_proxy'] = article_worker_proxy
    if article_max_connections is not None:
        client_kwargs['article_max_connections'] = article_max_connections
    # The caller names the exit (typically the WeChat one); an omitted proxy keeps
    # MPClient's own default, which is a direct connection.
    if proxy is not None:
        client_kwargs['proxy'] = proxy
    client = MPClient(**client_kwargs)
    image_service: ArticleImageService | None = None
    if enable_images:
        image_service = ArticleImageService(
            image_repo=storage.images,
            file_storage=S3FileStorage(),
            transaction=storage.transaction,
        )
    downloader = ArticleDownloader(
        client=client,
        storage=storage,
        image_store=image_service,
        image_workers=image_workers,
        enable_image_worker=enable_image_worker,
    )
    return AppContainer(
        storage=storage,
        client=client,
        image_service=image_service,
        downloader=downloader,
    )


def _source_proxy(settings: Mapping[str, Any] | None, source: str) -> str:
    """The proxy for ``source``, or ``''`` when the settings are not loaded yet.

    An empty settings mapping means the caller has no media settings at hand
    (a test, or an early boot); connect directly rather than guess an exit.
    """
    if not settings:
        return ''
    if source not in PROXY_SOURCES and source not in PROXY_ALIASES:
        # 未知来源：没有自己的出口可读，退回默认代理。
        return str(settings.get('proxy_default') or '')
    return resolve_proxy(settings, source)


__all__ = ['AppContainer', 'build_downloader_container', 'build_sync_container']
