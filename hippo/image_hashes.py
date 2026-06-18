"""Helpers for computing and backfilling article image content hashes."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from typing import Any

from .http import MPClient
from .s3 import build_image_key, fetch_object_bytes, get_s3_client, upload_object_bytes
from .storage import PostgresStorage, fetchone_row, open_storage

IMAGE_HASH_ALGO = 'sha256'
logger = logging.getLogger('hippo.image_hashes')


def compute_image_content_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def fetch_image_bytes(
    storage: PostgresStorage,
    image_id: int,
    *,
    allow_origin_fetch: bool = True,
) -> tuple[bytes, str]:
    row = fetchone_row(
        storage,
        (
            'SELECT i.content_type, i.s3_key, i.orig_url, a.link AS referer'
            ' FROM article_images i'
            ' JOIN articles a ON a.id = i.article_pk'
            ' WHERE i.id = %s'
        ),
        [image_id],
    )
    if not row:
        raise LookupError(f'Image {image_id} not found')
    content_type = row.get('content_type')
    s3_key = row.get('s3_key')
    if s3_key:
        bundle = get_s3_client()
        if bundle:
            config, client = bundle
            try:
                payload, s3_content_type = fetch_object_bytes(
                    client,
                    bucket=config.bucket,
                    key=str(s3_key),
                )
                resolved_type = s3_content_type or content_type or 'application/octet-stream'
                return payload, resolved_type
            except Exception as exc:
                logger.warning('S3 image fetch failed (id=%s key=%s): %s', image_id, s3_key, exc)
                if not allow_origin_fetch:
                    raise RuntimeError(f'Stored image fetch failed for {image_id}') from exc
    if not allow_origin_fetch:
        raise RuntimeError(f'Image {image_id} is not stored yet')
    orig_url = row.get('orig_url')
    if not orig_url:
        raise LookupError(f'Image {image_id} data missing')
    referer = row.get('referer')
    try:
        payload, fetched_type = _download_image_from_origin(str(orig_url), referer=referer)
    except Exception as exc:
        logger.warning('Origin image fetch failed (id=%s url=%s): %s', image_id, orig_url, exc)
        raise RuntimeError(f'Image fetch failed for {image_id}') from exc
    resolved_type = fetched_type or content_type or 'application/octet-stream'
    _store_image_to_s3_async(
        image_id=image_id,
        payload=payload,
        content_type=resolved_type,
        s3_key=str(s3_key) if s3_key else None,
    )
    return payload, resolved_type


def ensure_image_hash(
    storage: PostgresStorage,
    image_id: int,
    *,
    allow_origin_fetch: bool = True,
) -> dict[str, Any]:
    row = storage.images.get_image_hash(image_id)
    if not row:
        raise LookupError(f'Image {image_id} not found')
    if row.get('hash_algo') == IMAGE_HASH_ALGO and row.get('content_hash'):
        return {
            'image_id': image_id,
            'hash_algo': str(row['hash_algo']),
            'content_hash': str(row['content_hash']),
        }
    payload, _content_type = fetch_image_bytes(
        storage,
        image_id,
        allow_origin_fetch=allow_origin_fetch,
    )
    content_hash = compute_image_content_hash(payload)
    storage.images.save_image_hash(
        image_id=image_id,
        hash_algo=IMAGE_HASH_ALGO,
        content_hash=content_hash,
    )
    return {
        'image_id': image_id,
        'hash_algo': IMAGE_HASH_ALGO,
        'content_hash': content_hash,
    }


def ensure_image_hash_by_id(
    pg_dsn: str,
    image_id: int,
    *,
    allow_origin_fetch: bool = True,
) -> dict[str, Any]:
    with PostgresStorage(pg_dsn) as storage, storage.transaction():
        return ensure_image_hash(
            storage,
            image_id,
            allow_origin_fetch=allow_origin_fetch,
        )


def _download_image_from_origin(
    orig_url: str,
    *,
    referer: str | None,
) -> tuple[bytes, str | None]:
    async def _run() -> tuple[bytes, str | None]:
        async with MPClient() as client:
            return await client.download_binary_with_type(orig_url, referer=referer)

    return asyncio.run(_run())


def _store_image_to_s3_async(
    *,
    image_id: int,
    payload: bytes,
    content_type: str | None,
    s3_key: str | None,
) -> None:
    def _worker() -> None:
        bundle = get_s3_client()
        if not bundle:
            return
        config, client = bundle
        resolved_key = s3_key or build_image_key(config.prefix, image_id, content_type)
        try:
            upload_object_bytes(
                client,
                bucket=config.bucket,
                key=resolved_key,
                payload=payload,
                content_type=content_type,
            )
            with open_storage() as storage, storage.transaction(), storage.conn.cursor() as cur:
                cur.execute(
                    """
                            UPDATE article_images
                            SET s3_key = %s,
                                content_type = %s,
                                updated_at = NOW()
                            WHERE id = %s
                            """,
                    (resolved_key, content_type, image_id),
                )
        except Exception as exc:
            logger.warning('S3 image store failed (id=%s key=%s): %s', image_id, resolved_key, exc)

    threading.Thread(target=_worker, daemon=True).start()


__all__ = [
    'IMAGE_HASH_ALGO',
    'compute_image_content_hash',
    'ensure_image_hash',
    'ensure_image_hash_by_id',
    'fetch_image_bytes',
]
