#!/usr/bin/env python3
"""端到端：列表入队 → 抓正文 → 落 PG。

用法::

    HIPPO_PG_DSN=postgresql://… WEIXIN_SDK_PATH=../weixin-rs/sdk/python \
      python3 scripts/wx_ingest.py --biz MjM5MDk1NzQzMQ== --key hqsbwx --limit 2

写入：``article_queue``（入队）→ ``articles`` / ``article_content`` /
``article_document`` / ``article_images``（正文阶段）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from hippo.downloader import ArticleDownloader
from hippo.file_storage import FileStorageError, S3FileStorage
from hippo.http import MPClient
from hippo.image_store import ArticleImageService
from hippo.storage import PostgresStorage
from hippo.weixin_source import WeixinSource
from hippo.weixin_worker import WeixinArticleSync


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--pg-dsn', default=os.environ.get('HIPPO_PG_DSN'))
    p.add_argument('--biz', required=True, help='PG accounts.biz（Mz…==）')
    p.add_argument('--key', required=True, help='微信号 alias 或 gh_')
    p.add_argument('--pages', type=int, default=1)
    p.add_argument('--limit', type=int, default=2, help='本次最多处理多少篇正文')
    p.add_argument('--batch-size', type=int, default=5)
    return p.parse_args()


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s %(message)s')
    args = parse_args()
    if not args.pg_dsn:
        print('缺少 HIPPO_PG_DSN / --pg-dsn', file=sys.stderr)
        return 2

    storage = PostgresStorage(args.pg_dsn)
    image_service = None
    try:
        image_service = ArticleImageService(
            image_repo=storage.images, file_storage=S3FileStorage(), transaction=storage.transaction
        )
    except FileStorageError as exc:
        print(f'未配置对象存储，跳过图片下载：{exc}', file=sys.stderr)

    async with MPClient() as client, WeixinSource() as source:
        downloader = ArticleDownloader(
            client=client,
            storage=storage,
            image_store=image_service,
            enable_image_worker=image_service is not None,
        )
        try:
            sync = WeixinArticleSync(
                storage=storage, source=source, downloader=downloader, batch_size=args.batch_size
            )
            listed = await sync.sync_account(biz=args.biz, source_key=args.key, pages=args.pages)
            print(f'列表 {listed.listed} 篇，新入队 {listed.enqueued} 条')
            drained = await sync.drain(limit=args.limit)
            print(f'正文：落库 {drained.ingested} 篇，失败 {drained.failed} 篇')
            print(f'队列状态：{storage.article_queue.stats()}')
        finally:
            await downloader.aclose()
    storage.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
