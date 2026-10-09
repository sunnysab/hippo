#!/usr/bin/env python3
"""删除 adhoc 里的重复文章（旧 Re-fetch 的产物）。

Re-fetch 曾经把重抓结果写成 `biz='adhoc'` 的新行，于是同一篇链接在真实账号下和 adhoc 下各有一份。
adhoc 没有任何订阅者，这些副本在界面上永远看不到，只会占着正文、图片和对象存储。
（代码已修：现在重抓落回原行，不再产生新副本。）

只删「同一 link 在别的 biz 下也存在」的行；只有在 adhoc 下存在的（真正手贴的链接）保留。

用法::

    HIPPO_PG_DSN=… python3 scripts/drop_adhoc_duplicates.py            # 只报告
    HIPPO_PG_DSN=… python3 scripts/drop_adhoc_duplicates.py --apply    # 删除（含 S3 对象）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from typing import Any

from hippo.file_storage import S3FileStorage
from hippo.storage import PostgresStorage

CANDIDATE_SQL = """
SELECT a.id, a.title,
       (SELECT count(*) FROM article_images i WHERE i.article_pk = a.id) AS images,
       (SELECT count(*) FROM article_images i WHERE i.article_pk = a.id AND coalesce(i.s3_key, '') <> '') AS s3_images
  FROM articles a
 WHERE a.biz = 'adhoc'
   AND EXISTS (SELECT 1 FROM articles b WHERE b.link = a.link AND b.biz <> 'adhoc')
 ORDER BY a.id
"""

KEYS_SQL = """
SELECT i.id, i.s3_key
  FROM article_images i
 WHERE i.article_pk = ANY(%s) AND coalesce(i.s3_key, '') <> ''
"""

SHARED_KEY_SQL = 'SELECT s3_key FROM article_images WHERE s3_key = ANY(%s) AND id <> ALL(%s)'

_IMAGE_ID_IN_KEY = re.compile(r'/(\d+)\.[a-z0-9]+$')


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--pg-dsn', default=os.environ.get('HIPPO_PG_DSN'))
    p.add_argument('--apply', action='store_true', help='真的删除；不加则只报告')
    return p.parse_args()


def log(message: str) -> None:
    print(f'[adhoc] {message}', file=sys.stderr, flush=True)


def _image_id_of(key: str) -> int | None:
    matched = _IMAGE_ID_IN_KEY.search(key)
    return int(matched.group(1)) if matched else None


async def main() -> int:
    args = parse_args()
    if not args.pg_dsn:
        log('缺少 HIPPO_PG_DSN / --pg-dsn')
        return 2

    async with PostgresStorage(args.pg_dsn) as storage:
        async with storage.conn.cursor() as cur:
            await cur.execute(CANDIDATE_SQL)
            rows = await cur.fetchall()
        await storage.rollback()
        if not rows:
            log('没有可删的重复文章')
            return 0

        ids = [int(row[0]) for row in rows]
        async with storage.conn.cursor() as cur:
            await cur.execute(KEYS_SQL, [ids])
            key_rows = await cur.fetchall()
        await storage.rollback()
        image_ids = {int(row[0]) for row in key_rows}
        keys = [str(row[1]) for row in key_rows]

        # 对象键里就带着 article_images.id，所以键只可能属于它自己那一行；
        # 仍然显式校验一次，避免误删别的文章共用的对象。
        mismatched = [(int(image_id), key) for image_id, key in key_rows if _image_id_of(str(key)) != int(image_id)]
        if mismatched:
            log(f'对象键与图片行不匹配，放弃：{mismatched[:3]}')
            return 1
        async with storage.conn.cursor() as cur:
            await cur.execute(SHARED_KEY_SQL, [keys, sorted(image_ids)])
            shared = await cur.fetchall()
        await storage.rollback()
        if shared:
            log(f'有对象被别的文章共用，放弃：{[row[0] for row in shared][:3]}')
            return 1

        log(f'重复文章 {len(rows)} 篇，图片行 {sum(int(row[2]) for row in rows)} 个（其中 {len(keys)} 个已上传 S3）')
        for row in rows:
            log(f'· #{row[0]} {str(row[1])[:32]}（图片 {row[2]}，S3 {row[3]}）')

        if not args.apply:
            log('只报告，未删除；确认后加 --apply')
            return 0

        deleted_objects = await asyncio.to_thread(S3FileStorage().delete_objects, keys)
        async with storage.transaction():
            async with storage.conn.cursor() as cur:
                await cur.execute('DELETE FROM articles WHERE id = ANY(%s)', [ids])
        log(f'已删除 {len(rows)} 篇重复文章、{deleted_objects}/{len(keys)} 个对象（图片/正文/文档随行级联）')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
