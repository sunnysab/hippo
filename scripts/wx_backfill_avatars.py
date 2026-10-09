#!/usr/bin/env python3
"""回填公众号头像：从已入库正文的 daemon 原始响应里取 ``ori_head_img_url``。

daemon 抓正文时会顺带返回该号的头像（132px 圆图，``ori_head_img_url``），但我们只把它
留在 ``article_document.raw_json`` 里，``accounts.round_head_img`` 一直是空的（那里只
有「搜索添加」才会写）。于是 ``/api/account/{biz}/avatar`` 既没缓存也没来源，只能 404，
列表里就是一个空头像位。

别名/gh_ 直接入库的号（没走搜索）都能这样补齐，不用再打 searchcontact。

用法::

    HIPPO_PG_DSN=… python3 scripts/wx_backfill_avatars.py --dry-run
    HIPPO_PG_DSN=… python3 scripts/wx_backfill_avatars.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from hippo.storage import PostgresStorage

# 每个号取最新一篇带 raw_json 的正文；没有 raw_json 的文档（老数据）跳过。
CANDIDATE_SQL = """
SELECT a.biz, a.nickname, u.url
  FROM accounts a
  CROSS JOIN LATERAL (
      SELECT d.raw_json->>'ori_head_img_url' AS url
        FROM articles ar
        JOIN article_document d ON d.article_pk = ar.id
       WHERE ar.biz = a.biz
         AND coalesce(d.raw_json->>'ori_head_img_url', '') <> ''
       ORDER BY ar.publish_at DESC NULLS LAST, ar.id DESC
       LIMIT 1
  ) u
 WHERE coalesce(a.round_head_img, '') = ''
 ORDER BY a.article_count DESC NULLS LAST
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--pg-dsn', default=os.environ.get('HIPPO_PG_DSN'))
    p.add_argument('--limit', type=int, default=None, help='本次最多处理多少个账号')
    p.add_argument('--dry-run', action='store_true', help='只打印将要写入的地址')
    return p.parse_args()


def log(message: str) -> None:
    print(f'[avatar] {message}', file=sys.stderr, flush=True)


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
        if args.limit is not None:
            rows = rows[: args.limit]
        log(f'待回填 {len(rows)} 个账号')
        for biz, nickname, url in rows:
            if args.dry_run:
                log(f'· {nickname}（{biz}）← {url}')
                continue
            async with storage.transaction():
                await storage.accounts.update_account_fields(str(biz), round_head_img=str(url))
            log(f'+ {nickname}（{biz}）← {url}')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
