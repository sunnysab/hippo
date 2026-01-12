"""Backfill missing image blobs in PostgreSQL."""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Iterable, Iterator, Optional

from wechatcli.http import MPClient
from wechatcli.storage import PostgresStorage


def _iter_missing_images(
    storage: PostgresStorage,
    *,
    limit: Optional[int],
) -> Iterator[dict]:
    query = """
        SELECT a.biz, a.article_id, a.link, i.orig_url
        FROM article_images i
        JOIN articles a ON a.id = i.article_pk
        WHERE i.data IS NULL AND i.orig_url IS NOT NULL
        ORDER BY a.id DESC, i.position ASC
    """
    params: list = []
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
    with storage.conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    for row in rows:
        yield {
            "biz": row[0],
            "article_id": row[1],
            "referer": row[2],
            "orig_url": row[3],
        }


def _download_with_retry(
    client: MPClient,
    url: str,
    *,
    referer: Optional[str],
    retries: int,
    sleep_base: float,
) -> tuple[bytes, Optional[str]]:
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            data, content_type = client.download_binary_with_type(url, referer=referer)
            return data, content_type
        except Exception as exc:
            last_exc = exc
            time.sleep(min(sleep_base * (2**attempt), 5.0))
    raise RuntimeError(str(last_exc)) from last_exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill missing image data in PostgreSQL")
    parser.add_argument("--pg-dsn", default=os.environ.get("WECHATCLI_PG_DSN"))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--sleep-base", type=float, default=0.5)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.pg_dsn:
        print("Missing --pg-dsn or WECHATCLI_PG_DSN", file=sys.stderr)
        return 2

    updated = 0
    skipped = 0
    failed = 0

    with PostgresStorage(args.pg_dsn) as storage, MPClient() as client:
        try:
            for item in _iter_missing_images(storage, limit=args.limit):
                orig_url = str(item["orig_url"])
                referer = item.get("referer")
                if args.dry_run:
                    print(f"DRY-RUN {orig_url}")
                    skipped += 1
                    continue
                try:
                    data, content_type = _download_with_retry(
                        client,
                        orig_url,
                        referer=referer,
                        retries=max(1, args.retries),
                        sleep_base=max(0.1, args.sleep_base),
                    )
                    storage.update_article_image_data(
                        item["biz"],
                        item["article_id"],
                        orig_url,
                        content_type,
                        data,
                    )
                    updated += 1
                    if updated % 20 == 0:
                        print(f"Updated {updated} images...")
                except Exception as exc:
                    failed += 1
                    print(f"FAILED {orig_url}: {exc}", file=sys.stderr)
        except KeyboardInterrupt:
            print("Interrupted. Exiting.")

    print(f"Done. updated={updated} skipped={skipped} failed={failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
