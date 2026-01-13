"""Backfill missing image blobs in PostgreSQL."""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys
import time
from typing import Iterable, Iterator, Optional

import httpx
from wechatcli.http import MPClient
from wechatcli.storage import PostgresStorage
from tqdm import tqdm


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


def _normalize_image_url(url: str) -> str:
    trimmed = url.strip().strip("\"'")
    if " " in trimmed:
        trimmed = trimmed.split(" ", 1)[0]
    if trimmed.endswith("\""):
        trimmed = trimmed.rstrip("\"")
    return trimmed


def _format_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        url = exc.request.url
        return f"{exc} status={status} url={url}"
    if isinstance(exc, httpx.RequestError):
        return f"{exc} url={exc.request.url}"
    return str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill missing image data in PostgreSQL")
    parser.add_argument("--pg-dsn", default=os.environ.get("WECHATCLI_PG_DSN"))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=8)
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
            items = list(_iter_missing_images(storage, limit=args.limit))
            if args.dry_run:
                for item in tqdm(items, desc="Backfill images", unit="img"):
                    print(f"DRY-RUN {item['orig_url']}")
                    skipped += 1
            else:
                worker_count = max(1, args.workers)

                def worker(item: dict) -> tuple[dict, bytes, Optional[str]]:
                    data, content_type = _download_with_retry(
                        client,
                        _normalize_image_url(str(item["orig_url"])),
                        referer=item.get("referer"),
                        retries=max(1, args.retries),
                        sleep_base=max(0.1, args.sleep_base),
                    )
                    return item, data, content_type

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=worker_count
                ) as executor:
                    future_map = {executor.submit(worker, item): item for item in items}
                    with tqdm(total=len(items), desc="Backfill images", unit="img") as bar:
                        for future in concurrent.futures.as_completed(future_map):
                            item = future_map[future]
                            orig_url = str(item["orig_url"])
                            try:
                                _, data, content_type = future.result()
                                storage.update_article_image_data(
                                    item["biz"],
                                    item["article_id"],
                                    orig_url,
                                    content_type,
                                    data,
                                )
                                updated += 1
                        except Exception as exc:
                            failed += 1
                            print(
                                f"FAILED {orig_url}: {_format_error(exc)}",
                                file=sys.stderr,
                            )
                            finally:
                                bar.update(1)
        except KeyboardInterrupt:
            print("Interrupted. Exiting.")

    print(f"Done. updated={updated} skipped={skipped} failed={failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
