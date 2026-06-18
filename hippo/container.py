"""Service container for wiring core dependencies."""

from __future__ import annotations

from dataclasses import dataclass

from .downloader import ArticleDownloader
from .file_storage import S3FileStorage
from .http import MPClient
from .image_store import ArticleImageService
from .storage import PostgresStorage
from .wechat_api import WeChatApiClient


@dataclass(slots=True)
class AppContainer:
    storage: PostgresStorage
    client: MPClient
    api_client: WeChatApiClient
    image_service: ArticleImageService | None
    downloader: ArticleDownloader | None

    async def __aenter__(self) -> AppContainer:
        await self.client.__aenter__()
        if self.downloader:
            await self.downloader.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        if self.downloader:
            await self.downloader.__aexit__(exc_type, exc, tb)
        await self.client.__aexit__(exc_type, exc, tb)


def build_sync_container(
    *,
    storage: PostgresStorage,
    enable_download: bool,
    enable_images: bool,
) -> AppContainer:
    client = MPClient()
    api_client = WeChatApiClient(client)
    image_service: ArticleImageService | None = None
    if enable_images:
        image_service = ArticleImageService(
            image_repo=storage.images,
            file_storage=S3FileStorage(),
            transaction=storage.transaction,
        )
    downloader = None
    if enable_download:
        downloader = ArticleDownloader(
            client=client,
            storage=storage,
            image_store=image_service,
            enable_image_worker=enable_images,
        )
    return AppContainer(
        storage=storage,
        client=client,
        api_client=api_client,
        image_service=image_service,
        downloader=downloader,
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
    client = MPClient(
        article_worker=article_worker,
        article_worker_proxy=article_worker_proxy,
        article_max_connections=article_max_connections,
    )
    api_client = WeChatApiClient(client)
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
        api_client=api_client,
        image_service=image_service,
        downloader=downloader,
    )


__all__ = ['AppContainer', 'build_downloader_container', 'build_sync_container']
