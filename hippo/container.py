"""Service container for wiring core dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .downloader import ArticleDownloader
from .file_storage import S3FileStorage
from .http import MPClient
from .image_store import ArticleImageService
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

    async def __aenter__(self) -> AppContainer:
        await self.client.__aenter__()
        if self.weixin_source:
            await self.weixin_source.__aenter__()
        if self.downloader:
            await self.downloader.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
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
) -> AppContainer:
    client = MPClient()
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
    return AppContainer(
        storage=storage,
        client=client,
        image_service=image_service,
        downloader=downloader,
        weixin_source=weixin_source,
        weixin_sync=weixin_sync,
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
) -> AppContainer:
    client_kwargs: dict[str, Any] = {}
    if article_worker is not None:
        client_kwargs['article_worker'] = article_worker
    if article_worker_proxy is not None:
        client_kwargs['article_worker_proxy'] = article_worker_proxy
    if article_max_connections is not None:
        client_kwargs['article_max_connections'] = article_max_connections
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


__all__ = ['AppContainer', 'build_downloader_container', 'build_sync_container']
