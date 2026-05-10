"""Image storage service bridging file storage and database metadata."""

from __future__ import annotations

import logging
from typing import Callable, Protocol

from .file_storage import FileStorage
from .image_hashes import IMAGE_HASH_ALGO, compute_image_content_hash
from .repositories import ImageRepository

logger = logging.getLogger(__name__)


class ArticleImageStore(Protocol):
    def store(
        self,
        *,
        biz: str,
        article_id: str,
        orig_url: str,
        content_type: str | None,
        data: bytes,
    ) -> None:
        ...

    def mark_failed(
        self,
        *,
        biz: str,
        article_id: str,
        orig_url: str,
        reason: str,
    ) -> None:
        ...


class ArticleImageService:
    def __init__(
        self,
        *,
        image_repo: ImageRepository,
        file_storage: FileStorage,
        transaction: Callable[[], object] | None = None,
    ) -> None:
        self._image_repo = image_repo
        self._file_storage = file_storage
        self._transaction = transaction

    def store(
        self,
        *,
        biz: str,
        article_id: str,
        orig_url: str,
        content_type: str | None,
        data: bytes,
    ) -> None:
        def _run() -> None:
            target = self._image_repo.get_article_image_target(biz, article_id, orig_url)
            if not target:
                return
            s3_key = self._file_storage.store_article_image(
                image_id=target.image_id,
                content_type=content_type,
                payload=data,
                key=target.s3_key,
            )
            self._image_repo.update_article_image_metadata(
                article_pk=target.article_pk,
                orig_url=orig_url,
                content_type=content_type,
                s3_key=s3_key,
            )
            try:
                content_hash = compute_image_content_hash(data)
                self._image_repo.save_image_hash(
                    image_id=target.image_id,
                    hash_algo=IMAGE_HASH_ALGO,
                    content_hash=content_hash,
                )
            except Exception:
                logger.warning('Failed to save hash for image %s', target.image_id)

        if self._transaction:
            with self._transaction():
                _run()
        else:
            _run()

    def mark_failed(
        self,
        *,
        biz: str,
        article_id: str,
        orig_url: str,
        reason: str,
    ) -> None:
        if self._transaction:
            with self._transaction():
                self._image_repo.mark_article_image_failed(biz, article_id, orig_url, reason)
        else:
            self._image_repo.mark_article_image_failed(biz, article_id, orig_url, reason)


__all__ = ['ArticleImageStore', 'ArticleImageService']
