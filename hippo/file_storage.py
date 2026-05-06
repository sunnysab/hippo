"""File storage interfaces and S3 implementation."""

from __future__ import annotations

from typing import Protocol

from .s3 import build_image_key, get_s3_client, upload_object_bytes, with_prefix


class FileStorageError(RuntimeError):
    pass


class FileStorage(Protocol):
    def store_article_image(
        self,
        *,
        image_id: int,
        content_type: str | None,
        payload: bytes,
        key: str | None = None,
    ) -> str:
        ...


class S3FileStorage:
    def __init__(self, *, prefix: str | None = None) -> None:
        bundle = get_s3_client()
        if not bundle:
            raise FileStorageError(
                'Missing S3 config. Set HIPPO_S3_ENDPOINT/HIPPO_S3_BUCKET/HIPPO_S3_ACCESS_KEY/HIPPO_S3_SECRET_KEY.'
            )
        config, client = bundle
        self._config = with_prefix(config, prefix)
        self._client = client

    def store_article_image(
        self,
        *,
        image_id: int,
        content_type: str | None,
        payload: bytes,
        key: str | None = None,
    ) -> str:
        resolved_key = key or build_image_key(self._config.prefix, image_id, content_type)
        upload_object_bytes(
            self._client,
            bucket=self._config.bucket,
            key=resolved_key,
            payload=payload,
            content_type=content_type,
        )
        return resolved_key

    def delete_objects(self, keys: list[str]) -> int:
        if not keys:
            return 0
        deleted = 0
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            objects = [{'Key': key} for key in batch]
            resp = self._client.delete_objects(
                Bucket=self._config.bucket,
                Delete={'Objects': objects, 'Quiet': True},
            )
            deleted += len(batch) - len(resp.get('Errors', []))
        return deleted


__all__ = ['FileStorage', 'FileStorageError', 'S3FileStorage']
