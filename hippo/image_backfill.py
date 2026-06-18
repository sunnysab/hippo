"""Background image backfill logic extracted from CLI to break circular dependency."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from urllib.parse import urlparse

import httpx
from tqdm import tqdm

from .file_storage import FileStorageError, S3FileStorage
from .http import MPClient
from .image_store import ArticleImageService
from .storage import PostgresStorage


def _build_image_store(storage: PostgresStorage) -> ArticleImageService | None:
    try:
        return ArticleImageService(
            image_repo=storage.images,
            file_storage=S3FileStorage(),
            transaction=storage.transaction,
        )
    except FileStorageError:
        return None


def _normalize_image_url(url: str) -> str:
    trimmed = url.strip().strip('"\'')
    if ' ' in trimmed:
        trimmed = trimmed.split(' ', 1)[0]
    if trimmed.endswith('"'):
        trimmed = trimmed.rstrip('"')
    return trimmed


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in ('http', 'https')


def _format_http_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f'{exc} status={exc.response.status_code} url={exc.request.url}'
    if isinstance(exc, httpx.RequestError):
        return f'{exc} url={exc.request.url}'
    return str(exc)


async def backfill_article_images(
    *,
    pg_dsn: str | None = None,
    limit: int | None = None,
    workers: int = 4,
    retries: int = 3,
    sleep_base: float = 0.5,
    retry_failed: bool = False,
    dry_run: bool = False,
    log: Callable[[str], None] | None = None,
) -> dict[str, int]:
    resolved_dsn = pg_dsn or os.environ.get('HIPPO_PG_DSN')
    if not resolved_dsn:
        raise RuntimeError('Missing PostgreSQL DSN. Set HIPPO_PG_DSN or pass --pg-dsn.')

    _log = log or (lambda msg: None)
    updated = 0
    skipped = 0
    failed = 0

    async def download_with_retry(client: MPClient, url: str, *, referer: str | None) -> tuple[bytes, str | None]:
        for attempt in range(1, retries + 1):
            try:
                return await client.download_binary_with_type(_normalize_image_url(url), referer=referer)
            except httpx.HTTPStatusError, httpx.RequestError:
                if attempt >= retries:
                    raise
                await asyncio.sleep(min(sleep_base * (2 ** (attempt - 1)), 5.0))

        raise RuntimeError('Download retry loop exited unexpectedly.')

    async with MPClient() as client:
        with PostgresStorage(resolved_dsn) as storage:
            image_store = _build_image_store(storage)
            if not image_store:
                raise RuntimeError('Failed to initialize image store (S3 not configured).')

            failed_clause = '' if retry_failed else ' AND i.failed_at IS NULL'
            count_query = (
                'SELECT COUNT(*)'
                ' FROM article_images i'
                ' JOIN articles a ON a.id = i.article_pk'
                f" WHERE (i.s3_key IS NULL OR i.s3_key = '') AND i.orig_url IS NOT NULL{failed_clause}"
            )
            with storage.conn.cursor() as cur:
                cur.execute(count_query)
                total_count = cur.fetchone()[0]

            if limit is not None:
                total_count = min(total_count, limit)

            base_query = (
                'SELECT i.id, a.biz, a.article_id, a.link, i.orig_url'
                ' FROM article_images i'
                ' JOIN articles a ON a.id = i.article_pk'
                f" WHERE (i.s3_key IS NULL OR i.s3_key = '') AND i.orig_url IS NOT NULL{failed_clause}"
            )
            order_clause = 'ORDER BY i.id DESC'

            if dry_run:
                progress = tqdm(total=total_count, desc='Backfill images', unit='img', dynamic_ncols=True, leave=True)
                try:
                    last_id: int | None = None
                    remaining = total_count
                    while remaining > 0:
                        with storage.conn.cursor() as cur:
                            current_limit = min(100, remaining)
                            if last_id is None:
                                cur.execute(f'{base_query} {order_clause} LIMIT %s', (current_limit,))
                            else:
                                cur.execute(
                                    f'{base_query} AND i.id < %s {order_clause} LIMIT %s', (last_id, current_limit)
                                )
                            rows = cur.fetchall()
                        if not rows:
                            break
                        for _, _, _, _, orig_url in rows:
                            _log(f'DRY-RUN {orig_url}')
                            skipped += 1
                            progress.update(1)
                        remaining -= len(rows)
                        last_id = rows[-1][0]
                finally:
                    progress.close()
            else:
                worker_count = max(1, workers)
                batch_size = worker_count * 4
                sem = asyncio.Semaphore(worker_count)

                async def process_item(
                    item: tuple,
                ) -> tuple[tuple, bytes | None, str | None, str | None]:
                    _, _biz, _article_id, referer, orig_url = item
                    normalized = _normalize_image_url(str(orig_url))
                    if not _is_http_url(normalized):
                        return item, None, None, f'Invalid URL scheme: {normalized}'
                    async with sem:
                        try:
                            data, content_type = await download_with_retry(
                                client,
                                normalized,
                                referer=str(referer) if referer else None,
                            )
                            return item, data, content_type, None
                        except Exception as exc:
                            return item, None, None, _format_http_error(exc)

                progress = tqdm(total=total_count, desc='Backfill images', unit='img', dynamic_ncols=True, leave=True)
                try:
                    last_id = None
                    remaining = total_count
                    while remaining > 0:
                        with storage.conn.cursor() as cur:
                            current_limit = min(batch_size, remaining)
                            if last_id is None:
                                cur.execute(f'{base_query} {order_clause} LIMIT %s', (current_limit,))
                            else:
                                cur.execute(
                                    f'{base_query} AND i.id < %s {order_clause} LIMIT %s', (last_id, current_limit)
                                )
                            batch = cur.fetchall()
                        if not batch:
                            break

                        tasks = [asyncio.create_task(process_item(item)) for item in batch]
                        for task_coro in asyncio.as_completed(tasks):
                            item, data, content_type, error = await task_coro
                            _, biz, article_id, _, orig_url = item
                            try:
                                if error:
                                    raise RuntimeError(error)
                                image_store.store(
                                    biz=biz,
                                    article_id=article_id,
                                    orig_url=str(orig_url),
                                    content_type=content_type,
                                    data=data,
                                )
                                updated += 1
                            except Exception as exc:
                                failed += 1
                                image_store.mark_failed(
                                    biz=biz,
                                    article_id=article_id,
                                    orig_url=str(orig_url),
                                    reason=str(exc),
                                )
                                _log(f'FAILED {orig_url}: {_format_http_error(exc)}')
                            finally:
                                progress.update(1)
                        remaining -= len(batch)
                        last_id = batch[-1][0]
                finally:
                    progress.close()

    return {'updated': updated, 'skipped': skipped, 'failed': failed}


__all__ = ['backfill_article_images']
