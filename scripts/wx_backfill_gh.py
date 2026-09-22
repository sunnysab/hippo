#!/usr/bin/env python3
"""为 accounts 回填 gh_id：拿该号任意一篇文章的短链去问详情，响应里的 user_name 就是 gh_。

为什么不用 alias 解析：`searchcontact`（微信号 → 陌生人 → gh_）会被限流，
一次全量跑就能把它打挂几十分钟（2026-09-22 实测）。而详情接口不吃这个限制，
且库里已经有 152 万篇文章，几乎每个账号都能取到 URL。

用法::

    HIPPO_PG_DSN=… WEIXIN_SDK_PATH=… \
      python3 scripts/wx_backfill_gh.py --limit 10          # 小批量验证
    HIPPO_PG_DSN=… python3 scripts/wx_backfill_gh.py        # 全量（可中断续跑）
    … --via-alias                                          # 没文章的账号改走 alias（慢，谨慎）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from hippo.storage import PostgresStorage
from hippo.weixin_source import WeixinSource

CANDIDATE_SQL = """
SELECT a.biz, a.nickname, u.url
  FROM accounts a
  CROSS JOIN LATERAL (
      SELECT ar.link AS url
        FROM articles ar
       WHERE ar.biz = a.biz AND ar.link LIKE '%%/s/%%'
       ORDER BY ar.publish_at DESC NULLS LAST, ar.id DESC
       LIMIT %s
  ) u
 WHERE a.gh_id IS NULL
   AND NOT a.is_disabled
 ORDER BY a.article_count DESC NULLS LAST
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--pg-dsn', default=os.environ.get('HIPPO_PG_DSN'))
    p.add_argument('--batch-size', type=int, default=5, help='每批 URL 数（daemon 的 [articles].batch_size）')
    p.add_argument('--limit', type=int, default=None, help='本次最多处理多少个账号')
    p.add_argument('--via-alias', action='store_true', help='没有文章的账号也试 alias（searchcontact，慢）')
    p.add_argument('--candidates', type=int, default=3, help='每个账号最多试几篇文章')
    return p.parse_args()


def log(message: str) -> None:
    print(f'[backfill gh_id] {message}', file=sys.stderr, flush=True)


async def main() -> int:
    args = parse_args()
    if not args.pg_dsn:
        log('缺少 HIPPO_PG_DSN / --pg-dsn')
        return 2
    storage = PostgresStorage(args.pg_dsn)
    with storage.conn.cursor() as cur:
        cur.execute(CANDIDATE_SQL, (max(args.candidates, 1),))
        rows = cur.fetchall()
    storage.commit()

    # 一个账号可以有多个候选文章：某一篇已删/隐私拿不到 user_name 时换下一篇
    pending: dict[str, dict[str, Any]] = {}
    for biz, nickname, url in rows:
        entry = pending.setdefault(biz, {'nickname': nickname, 'urls': []})
        entry['urls'].append(url)
    # 没有任何文章可用的账号
    with storage.conn.cursor() as cur:
        cur.execute(
            "SELECT a.biz, a.nickname, a.alias FROM accounts a "
            "WHERE a.gh_id IS NULL AND NOT a.is_disabled "
            "AND NOT EXISTS (SELECT 1 FROM articles ar WHERE ar.biz = a.biz AND ar.link LIKE '%%/s/%%')"
        )
        without_url = cur.fetchall()
    storage.commit()
    if args.limit is not None:
        pending = dict(list(pending.items())[: args.limit])
    log(f'待回填 {len(pending) + len(without_url)} 个账号：可用文章 {len(pending)}，无文章 {len(without_url)}')

    filled = failed = 0
    async with WeixinSource() as source:
        while pending:
            chunk = list(pending.items())[: args.batch_size]
            urls = [entry['urls'][0] for _biz, entry in chunk]
            try:
                bodies = await source.fetch_bodies(urls)
            except Exception as exc:
                log(f'批次失败（{exc}）：{urls[0][:60]}…')
                failed += len(chunk)
                pending = dict(list(pending.items())[args.batch_size :])
                continue
            by_url = {body.url: body for body in bodies}
            for biz, entry in chunk:
                body = by_url.get(entry['urls'][0])
                gh_id = getattr(body, 'user_name', '') if body else ''
                if gh_id.startswith('gh_'):
                    with storage.transaction():
                        storage.accounts.set_gh_id(biz, gh_id)
                    filled += 1
                    log(f'+ {entry["nickname"]} → {gh_id}')
                    del pending[biz]
                    continue
                entry['urls'].pop(0)
                if not entry['urls']:
                    failed += 1
                    log(f'✗ {entry["nickname"]}：{args.candidates} 篇都没拿到 user_name')
                    del pending[biz]
            log(f'剩余 {len(pending)}，成功 {filled}，失败 {failed}')

    if args.via_alias and without_url:
        log(f'改用 alias 解析 {len(without_url)} 个无文章账号（searchcontact，注意限流）')
        async with WeixinSource() as source:
            for biz, nickname, alias in without_url:
                if not alias:
                    continue
                try:
                    items = await source.list_articles(alias, biz, pages=1)
                except Exception as exc:
                    log(f'✗ {nickname}（{alias}）：{exc}')
                    failed += 1
                    continue
                if items:
                    log(f'~ {nickname}（{alias}）列表可用，但 gh_ 仍需单独解析')
                await asyncio.sleep(20)

    storage.close()
    log(f'完成：成功 {filled}，失败 {failed}')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
