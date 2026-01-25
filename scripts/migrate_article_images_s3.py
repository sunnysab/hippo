"""Migrate article_images blobs into S3 and store keys back to PostgreSQL."""

from __future__ import annotations

import argparse
import os
import sys
import threading
from queue import Queue
from datetime import datetime, timezone
from typing import Iterable

from hippo.env import load_env
from hippo.s3 import (
    build_image_key,
    load_s3_config,
    upload_object_bytes,
    with_prefix,
)
from hippo.storage import PostgresStorage
from tqdm import tqdm


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _log(message: str) -> None:
    print(f'[migrate s3] {message}', file=sys.stderr, flush=True)


def _iter_images(
    storage: PostgresStorage, *, limit: int | None, chunk_size: int
) -> Iterable[tuple[int, str | None, bytes]]:
    query = (
        'SELECT id, content_type, data '
        'FROM article_images '
        'WHERE data IS NOT NULL AND (s3_key IS NULL OR s3_key = \'\') '
        'ORDER BY id ASC'
    )
    params: list = []
    if limit is not None:
        query += ' LIMIT %s'
        params.append(limit)
    with storage.conn.cursor(name='article_images_cursor') as cur:
        cur.itersize = chunk_size
        cur.execute(query, params)
        for row in cur:
            image_id, content_type, data = row
            if isinstance(data, memoryview):
                payload = data.tobytes()
            else:
                payload = bytes(data)
            yield image_id, content_type, payload


def _count_images(storage: PostgresStorage) -> int:
    query = (
        'SELECT COUNT(1) '
        'FROM article_images '
        'WHERE data IS NOT NULL AND (s3_key IS NULL OR s3_key = \'\')'
    )
    with storage.conn.cursor() as cur:
        cur.execute(query)
        row = cur.fetchone()
    return int(row[0]) if row else 0


def _update_s3_key(
    storage: PostgresStorage,
    *,
    image_id: int,
    s3_key: str,
    prune_db: bool,
) -> None:
    if prune_db:
        query = (
            'UPDATE article_images '
            'SET s3_key = %s, data = NULL, updated_at = %s '
            'WHERE id = %s'
        )
    else:
        query = (
            'UPDATE article_images '
            'SET s3_key = %s, updated_at = %s '
            'WHERE id = %s'
        )
    with storage.conn.cursor() as cur:
        cur.execute(query, (s3_key, _utc_now(), image_id))
    storage.conn.commit()


def _has_data_column(storage: PostgresStorage) -> bool:
    with storage.conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'article_images' AND column_name = 'data'
            """
        )
        return cur.fetchone() is not None


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(description='Migrate article_images blobs into S3')
    parser.add_argument('--pg-dsn', default=os.environ.get('HIPPO_PG_DSN'))
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--prefix', default=None)
    parser.add_argument('--prune-db', action='store_true')
    parser.add_argument('--timeout', type=float, default=5.0)
    parser.add_argument('--log-every', type=int, default=1)
    parser.add_argument('--chunk-size', type=int, default=200)
    parser.add_argument('--workers', type=int, default=10)
    parser.add_argument('--queue-size', type=int, default=None)
    args = parser.parse_args()

    if not args.pg_dsn:
        print('Missing --pg-dsn or HIPPO_PG_DSN', file=sys.stderr)
        return 2

    with PostgresStorage(args.pg_dsn) as check_storage:
        if not _has_data_column(check_storage):
            _log('article_images.data is missing. Nothing to migrate.')
            return 0

    base_config = load_s3_config()
    if not base_config:
        print(
            'Missing S3 config. Set HIPPO_S3_ENDPOINT/HIPPO_S3_BUCKET/HIPPO_S3_ACCESS_KEY/HIPPO_S3_SECRET_KEY',
            file=sys.stderr,
        )
        return 2
    config = with_prefix(base_config, args.prefix)

    if args.dry_run:
        _log('DRY-RUN enabled, no data will be uploaded or updated.')

    updated = 0
    skipped = 0
    failed = 0

    import boto3
    from botocore.config import Config as BotoConfig

    chunk_size = max(1, args.chunk_size)
    workers = max(1, args.workers)
    if args.queue_size is None:
        queue_size = chunk_size
    else:
        queue_size = max(1, args.queue_size)
    queue_size = max(workers, queue_size)
    _log(
        'S3 config loaded '
        f'endpoint={config.endpoint} bucket={config.bucket} prefix={config.prefix} '
        f'timeout={args.timeout}s chunk_size={chunk_size} workers={workers} '
        f'queue_size={queue_size}'
    )

    client_config = BotoConfig(
        s3={'addressing_style': 'path'},
        connect_timeout=args.timeout,
        read_timeout=args.timeout,
    )

    def _build_client():
        session = boto3.session.Session()
        return session.client(
            's3',
            endpoint_url=config.endpoint,
            aws_access_key_id=config.access_key,
            aws_secret_access_key=config.secret_key,
            region_name=config.region,
            config=client_config,
        )

    _log('S3 client factory ready.')

    stats_lock = threading.Lock()
    progress_lock = threading.Lock()

    def _bump(counter: str) -> None:
        nonlocal updated, skipped, failed
        with stats_lock:
            if counter == 'updated':
                updated += 1
            elif counter == 'skipped':
                skipped += 1
            elif counter == 'failed':
                failed += 1

    def _worker_loop(worker_id: int, q: Queue, total: int, progress) -> None:
        client = _build_client()
        storage = PostgresStorage(args.pg_dsn)
        try:
            while True:
                item = q.get()
                if item is None:
                    break
                idx, image_id, content_type, payload = item
                s3_key = build_image_key(config.prefix, image_id, content_type)
                if args.log_every > 0 and idx % args.log_every == 0:
                    _log(
                        f'[{idx}/{total}] worker={worker_id} uploading id={image_id} '
                        f'key={s3_key} bytes={len(payload)} type={content_type or ""}'
                    )
                if args.dry_run:
                    _log(f'DRY-RUN {image_id} -> {s3_key}')
                    _bump('skipped')
                    with progress_lock:
                        progress.update(1)
                    continue
                try:
                    upload_object_bytes(
                        client,
                        bucket=config.bucket,
                        key=s3_key,
                        payload=payload,
                        content_type=content_type,
                    )
                    _update_s3_key(storage, image_id=image_id, s3_key=s3_key, prune_db=args.prune_db)
                    _bump('updated')
                except Exception as exc:
                    _bump('failed')
                    print(f'FAILED {image_id} -> {s3_key}: {exc}', file=sys.stderr)
                finally:
                    with progress_lock:
                        progress.update(1)
        finally:
            storage.close()

    with PostgresStorage(args.pg_dsn) as read_storage:
        _log('Counting images...')
        total = _count_images(read_storage)
        if args.limit is not None:
            total = min(total, args.limit)
        _log(f'Images to migrate: {total}')
        _log('Opening server-side cursor...')
        items = _iter_images(read_storage, limit=args.limit, chunk_size=chunk_size)
        q: Queue = Queue(maxsize=queue_size)
        workers_list: list[threading.Thread] = []
        with tqdm(total=total, desc='Migrate images', unit='img') as bar:
            for worker_id in range(1, workers + 1):
                t = threading.Thread(
                    target=_worker_loop, args=(worker_id, q, total, bar), daemon=True
                )
                t.start()
                workers_list.append(t)

            for idx, (image_id, content_type, payload) in enumerate(items, start=1):
                q.put((idx, image_id, content_type, payload))

            for _ in workers_list:
                q.put(None)
            for t in workers_list:
                t.join()

    print(f'Done. updated={updated} skipped={skipped} failed={failed}')
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
