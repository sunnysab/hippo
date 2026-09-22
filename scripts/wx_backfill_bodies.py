#!/usr/bin/env python3
"""给「从未抓过正文」的文章补正文（走客户端协议，一份数据两用）。

缺口来自历史：库里有 150 万+ 篇文章，其中约 6.4 千篇只有标题/链接、没有 `article_content`
行（当年列表入库后详情没抓成）。这里用它们的**永久短链**去问详情，落
`article_content` + `article_document`，并顺手把响应里的 `user_name` 写回
`accounts.gh_id`（如果那个号还没解析过）—— 同一份响应两处都用上，不白抓。

节奏交给 daemon 的 `[articles]` 闸门（5~10 篇/分钟），脚本只负责分批与落库；失败记进
`article_download_attempts`，重跑时跳过（`--retry-failed` 可强制重试）。

用法::

    HIPPO_PG_DSN=… WEIXIN_SDK_PATH=… python3 scripts/wx_backfill_bodies.py --limit 5
    HIPPO_PG_DSN=… WEIXIN_SDK_PATH=… python3 scripts/wx_backfill_bodies.py        # 全量
    … --retry-failed          # 连之前失败的一起重试
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from hippo.downloader import ArticleDownloader
from hippo.file_storage import FileStorageError, S3FileStorage
from hippo.http import MPClient
from hippo.image_store import ArticleImageService
from hippo.models import ArticleRecord
from hippo.storage import PostgresStorage
from hippo.weixin_source import WeixinSource

# 每轮从库里取多少篇候选（一轮内再按 --batch-size 分批请求）
FETCH_WINDOW = 200

MISSING_SQL = """
SELECT a.id, a.biz, a.article_id, a.title, a.item_show_type, a.author, a.digest,
       a.link, a.source_url, a.publish_at, a.raw_json
  FROM articles a
  LEFT JOIN article_content c ON c.article_pk = a.id
 WHERE c.article_pk IS NULL
   AND a.link LIKE '%%/s/%%'
   {retry_clause}
 ORDER BY a.publish_at DESC NULLS LAST, a.id DESC
 LIMIT %s
"""

IMAGE_META_SQL = (
    'SELECT orig_url, s3_key, content_type, hash_algo, content_hash '
    'FROM article_images WHERE article_pk = %s AND orig_url IS NOT NULL'
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--pg-dsn', default=os.environ.get('HIPPO_PG_DSN'))
    p.add_argument('--batch-size', type=int, default=5, help='每批 URL 数（与 daemon [articles].batch_size 对齐）')
    p.add_argument('--limit', type=int, default=None, help='本次最多处理多少篇')
    p.add_argument('--retry-failed', action='store_true', help='重试之前失败过的（默认跳过）')
    p.add_argument('--dry-run', action='store_true')
    return p.parse_args()


def log(message: str) -> None:
    print(f'[backfill bodies] {message}', file=sys.stderr, flush=True)


def load_image_meta(storage: PostgresStorage, article_pk: int) -> dict[str, tuple]:
    """记下已有图片的存储信息：ingest 会重建 image 行，之后要按 orig_url 还原。"""
    with storage.conn.cursor() as cur:
        cur.execute(IMAGE_META_SQL, (article_pk,))
        rows = cur.fetchall()
    storage.rollback()
    return {str(url): (s3, ctype, algo, chash) for url, s3, ctype, algo, chash in rows if url}


def restore_image_meta(storage: PostgresStorage, article_pk: int, meta: dict[str, tuple]) -> None:
    if not meta:
        return
    with storage.conn.cursor() as cur:
        for orig_url, (s3_key, ctype, algo, chash) in meta.items():
            if not s3_key:
                continue
            cur.execute(
                """
                UPDATE article_images
                   SET s3_key = %s,
                       content_type = COALESCE(content_type, %s),
                       hash_algo = COALESCE(hash_algo, %s),
                       content_hash = COALESCE(content_hash, %s)
                 WHERE article_pk = %s AND orig_url = %s
                   AND (s3_key IS NULL OR s3_key = '')
                """,
                (s3_key, ctype, algo, chash, article_pk, orig_url),
            )
    storage.commit()


def record_attempt(storage: PostgresStorage, *, biz: str, article_id: str, error: str, retryable: bool) -> None:
    with storage.transaction(), storage.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO article_download_attempts
                (biz, article_id, attempts, last_error, last_attempt_at, created_at, error_type, retryable)
            VALUES (%s, %s, 1, %s, NOW(), NOW(), 'body_backfill', %s)
            ON CONFLICT (biz, article_id) DO UPDATE SET
                attempts = article_download_attempts.attempts + 1,
                last_error = EXCLUDED.last_error,
                last_attempt_at = NOW(),
                retryable = EXCLUDED.retryable
            """,
            (biz, article_id, error[:500], retryable),
        )


def to_record(row: tuple) -> ArticleRecord:
    (pk, biz, article_id, title, item_show_type, author, digest,
     link, source_url, publish_at, raw_json) = row
    return ArticleRecord(
        biz=str(biz),
        article_id=str(article_id),
        title=str(title or ''),
        item_show_type=item_show_type,
        author=author,
        digest=digest,
        cover=None,
        link=str(link),
        source_url=source_url,
        publish_at=publish_at,
        raw=json.loads(raw_json) if raw_json else {},
    ), int(pk)


async def main() -> int:
    args = parse_args()
    if not args.pg_dsn:
        log('缺少 HIPPO_PG_DSN / --pg-dsn')
        return 2

    storage = PostgresStorage(args.pg_dsn)
    retry_clause = '' if args.retry_failed else (
        'AND NOT EXISTS (SELECT 1 FROM article_download_attempts t '
        'WHERE t.biz = a.biz AND t.article_id = a.article_id)'
    )
    image_service = None
    try:
        image_service = ArticleImageService(
            image_repo=storage.images, file_storage=S3FileStorage(), transaction=storage.transaction
        )
    except FileStorageError as exc:
        log(f'未配置对象存储，跳过图片登记：{exc}')

    filled = failed = 0
    async with MPClient() as client, WeixinSource() as source:
        downloader = ArticleDownloader(
            client=client, storage=storage, image_store=image_service, enable_image_worker=False
        )
        try:
            while True:
                remaining = None if args.limit is None else args.limit - filled - failed
                if remaining is not None and remaining <= 0:
                    break
                fetch_size = FETCH_WINDOW if remaining is None else min(FETCH_WINDOW, remaining)
                with storage.conn.cursor() as cur:
                    cur.execute(MISSING_SQL.format(retry_clause=retry_clause), (fetch_size,))
                    rows = cur.fetchall()
                storage.rollback()
                if not rows:
                    break
                if args.dry_run:
                    for row in rows[:10]:
                        log(f'DRY-RUN {row[3]}  {row[7]}')
                    break
                log(f'本轮 {len(rows)} 篇（已补 {filled}，失败 {failed}）')

                for start in range(0, len(rows), args.batch_size):
                    chunk = rows[start : start + args.batch_size]
                    links = [str(row[7]) for row in chunk]
                    try:
                        bodies = await source.fetch_bodies(links)
                    except Exception as exc:
                        log(f'批次失败：{exc}')
                        for row in chunk:
                            record_attempt(
                                storage, biz=str(row[1]), article_id=str(row[2]),
                                error=str(exc), retryable=True,
                            )
                        failed += len(chunk)
                        continue
                    by_url = {body.url: body for body in bodies}
                    for row in chunk:
                        record, pk = to_record(row)
                        body = by_url.get(str(row[7]))
                        if body is None or not body.html:
                            record_attempt(
                                storage, biz=record.biz, article_id=record.article_id,
                                error='详情里没有正文', retryable=False,
                            )
                            failed += 1
                            continue
                        meta = load_image_meta(storage, pk)
                        try:
                            await downloader.ingest_body(
                                article=record,
                                html=body.html,
                                title=body.title or record.title,
                                item_show_type=body.item_show_type,
                                with_images=False,
                            )
                            restore_image_meta(storage, pk, meta)
                            storage.documents.save(
                                article_pk=pk,
                                source='api_backfill',
                                url_token=body.slug,
                                raw_html=body.html,
                                raw_json=json.loads(body.raw_json) if body.raw_json else None,
                            )
                            storage.commit()
                            filled += 1
                        except Exception as exc:
                            storage.rollback()
                            record_attempt(
                                storage, biz=record.biz, article_id=record.article_id,
                                error=str(exc), retryable=True,
                            )
                            failed += 1
                        # 顺手：该号还没解析过 gh_ 就用这条响应补上
                        if body.user_name.startswith('gh_'):
                            with storage.conn.cursor() as cur:
                                cur.execute(
                                    'UPDATE accounts SET gh_id = %s, updated_at = NOW() '
                                    'WHERE biz = %s AND (gh_id IS NULL OR gh_id = %s)',
                                    (body.user_name, record.biz, body.user_name),
                                )
                            storage.commit()
                    log(f'  进度 {min(start + args.batch_size, len(rows))}/{len(rows)}，成功 {filled}，失败 {failed}')
                if len(rows) < fetch_size:
                    break
        finally:
            await downloader.aclose()
    storage.close()
    log(f'完成：补 {filled} 篇，失败 {failed} 篇')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
