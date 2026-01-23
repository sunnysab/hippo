"""Migrate article_images blobs into S3 and store keys back to PostgreSQL."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Iterable

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
    storage: PostgresStorage, *, limit: int | None
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
    with storage.conn.cursor() as cur:
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


def main() -> int:
    parser = argparse.ArgumentParser(description='Migrate article_images blobs into S3')
    parser.add_argument('--pg-dsn', default=os.environ.get('HIPPO_PG_DSN'))
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--prefix', default=None)
    parser.add_argument('--prune-db', action='store_true')
    parser.add_argument('--timeout', type=float, default=5.0)
    parser.add_argument('--log-every', type=int, default=1)
    args = parser.parse_args()

    if not args.pg_dsn:
        print('Missing --pg-dsn or HIPPO_PG_DSN', file=sys.stderr)
        return 2

    base_config = load_s3_config()
    if not base_config:
        print('Missing S3 config. Set HIPPO_S3_ENDPOINT/HIPPO_S3_BUCKET/HIPPO_S3_ACCESS_KEY/HIPPO_S3_SECRET_KEY', file=sys.stderr)
        return 2
    config = with_prefix(base_config, args.prefix)

    if args.dry_run:
        _log('DRY-RUN enabled, no data will be uploaded or updated.')

    updated = 0
    skipped = 0
    failed = 0

    import boto3
    from botocore.config import Config as BotoConfig

    _log(
        'S3 config loaded '
        f'endpoint={config.endpoint} bucket={config.bucket} prefix={config.prefix} '
        f'timeout={args.timeout}s'
    )
    session = boto3.session.Session()
    _log('Creating S3 client...')
    client = session.client(
        's3',
        endpoint_url=config.endpoint,
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        region_name=config.region,
        config=BotoConfig(
            s3={'addressing_style': 'path'},
            connect_timeout=args.timeout,
            read_timeout=args.timeout,
        ),
    )
    _log('S3 client ready.')

    with PostgresStorage(args.pg_dsn) as storage:
        _log('Counting images...')
        total = _count_images(storage)
        if args.limit is not None:
            total = min(total, args.limit)
        _log(f'Images to migrate: {total}')
        items = _iter_images(storage, limit=args.limit)
        for idx, (image_id, content_type, payload) in enumerate(
            tqdm(items, total=total, desc='Migrate images', unit='img'),
            start=1,
        ):
            s3_key = build_image_key(config.prefix, image_id, content_type)
            if args.log_every > 0 and idx % args.log_every == 0:
                _log(
                    f'[{idx}/{total}] uploading id={image_id} key={s3_key} '
                    f'bytes={len(payload)} type={content_type or ""}'
                )
            if args.dry_run:
                _log(f'DRY-RUN {image_id} -> {s3_key}')
                skipped += 1
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
                updated += 1
            except Exception as exc:
                failed += 1
                print(f'FAILED {image_id} -> {s3_key}: {exc}', file=sys.stderr)

    print(f'Done. updated={updated} skipped={skipped} failed={failed}')
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
