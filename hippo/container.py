"""Service container for wiring core dependencies."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from .downloader import ArticleDownloader
from .file_storage import S3FileStorage
from .http import MPClient
from .image_store import ArticleImageService
from .logger import get_logger
from .media_settings import PROXY_ALIASES, PROXY_SOURCES, resolve_proxy
from .sources import SourceAdapter, SourceError
from .sources.podcast import FEED_SOURCE
from .sources.registry import MediaSources
from .sources.weixin import WeixinSourceAdapter
from .storage import PostgresStorage
from .weixin_daemon import SessionExpiredError, WeixinSource
from .weixin_ingest import WeixinArticleSync

logger = get_logger(__name__)

#: Failures that mean the WeChat daemon is unreachable or logged out, not a Hippo
#: bug. Anything else -- a missing SDK, a TypeError -- must propagate: a broken
#: container should stay loud instead of being mistaken for a daemon outage.
WEIXIN_UNAVAILABLE_ERRORS = (SessionExpiredError, OSError)


class _UnavailableSourceAdapter:
    """Stands in for a source whose client could not be opened this round.

    Registered under the same name so its accounts fail with the real reason
    (``failed`` in the report) instead of looking unsupported or synced, while the
    other sources keep running in the same round.
    """

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def sync_account(self, *, biz: str, source_key: str, pages: int = 1, offset: str = '') -> Any:
        raise SourceError(f'weixin source unavailable: {self._reason}')


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
    #: Why the WeChat daemon could not be opened this round, if it failed.
    weixin_unavailable: str | None = None
    #: Resources opened by ``__aenter__``, unwound in reverse on exit.
    _opened: AsyncExitStack | None = field(default=None, repr=False)

    async def __aenter__(self) -> AppContainer:
        opened = AsyncExitStack()
        await opened.__aenter__()
        self._opened = opened
        try:
            await opened.enter_async_context(self.client)
            if self.weixin_source:
                await self._open_weixin(opened)
            if self.downloader:
                await opened.enter_async_context(self.downloader)
            if self.media_sources:
                opened.push_async_callback(self.media_sources.aclose)
        except BaseException:
            # The context manager protocol never calls __aexit__ when __aenter__
            # raises, so release what was opened here. The start failure is what
            # matters: a cleanup error is logged, never allowed to replace it.
            self._opened = None
            try:
                await opened.aclose()
            except BaseException as cleanup_exc:
                logger.warning('Container cleanup after a failed start also failed: %s', cleanup_exc)
            raise
        return self

    async def _open_weixin(self, opened: AsyncExitStack) -> None:
        source = self.weixin_source
        if source is None:
            return
        try:
            await opened.enter_async_context(source)
        except BaseException as exc:
            # A failed enter may still have opened the RPC connection before it
            # raised; close it before deciding what to do with the error.
            try:
                await source.__aexit__(None, None, None)
            except BaseException as close_exc:
                logger.warning('Failed to close the half-open WeChat source: %s', close_exc)
            if not isinstance(exc, WEIXIN_UNAVAILABLE_ERRORS):
                # A missing SDK or a bug is not an outage: let it surface.
                raise
            # The daemon is unreachable or logged out. Do not fail the whole round.
            # Replace the adapter with one that fails explicitly, so the media
            # sources still run and WeChat accounts are never reported as synced.
            reason = str(exc) or type(exc).__name__
            self.weixin_unavailable = reason
            logger.warning('WeChat source unavailable: %s', reason)
            self.weixin_source = None
            self.weixin_sync = None
            self.adapters['weixin'] = _UnavailableSourceAdapter(reason)

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        opened = self._opened
        self._opened = None
        if opened is not None:
            await opened.aclose()


def build_sync_container(
    *,
    storage: PostgresStorage,
    enable_download: bool,
    enable_images: bool,
    media_settings: Mapping[str, Any] | None = None,
    sources: Collection[str] | None = None,
) -> AppContainer:
    # ``sources`` is the set of account sources this run selected. A run with no
    # WeChat account must not build or connect the daemon at all; ``None`` keeps
    # the old "build everything" default for callers that do not know yet.
    wants_weixin = sources is None or 'weixin' in sources

    # Article bodies, avatars and images are all WeChat traffic, so the client
    # uses the proxy configured for that source instead of one instance-wide exit.
    client = MPClient(proxy=_source_proxy(media_settings, 'weixin'))
    weixin_source = WeixinSource() if wants_weixin else None
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
    weixin_sync = (
        WeixinArticleSync(storage=storage, source=weixin_source, downloader=downloader)
        if weixin_source is not None
        else None
    )
    media_sources = MediaSources(storage=storage, settings=media_settings or {})
    adapters: dict[str, SourceAdapter] = {}
    if weixin_sync is not None:
        adapters['weixin'] = WeixinSourceAdapter(weixin_sync)
    if media_settings and not media_settings.get('enabled', True):
        # Turning transcription off only stops the sources that need audio/video.
        # A plain text feed (RSS with no enclosure) is listed and stored like any
        # article, so keep its adapter registered instead of reporting those
        # accounts as unsupported.
        feed_adapter = media_sources.adapters.get(FEED_SOURCE)
        if feed_adapter is not None:
            adapters[FEED_SOURCE] = feed_adapter
    else:
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
