#!/usr/bin/env python3
"""抽样校验：账号的 ``alias`` 能不能被 daemon 当列表来源用。

worker 的列表阶段需要 ``source_key``（微信号 alias 或 gh_）。PG ``accounts.biz``
是 ``Mz…==``，daemon 的列表接口不认，所以只能靠 alias（或先用文章 URL 反查 gh_）。

用法::

    HIPPO_PG_DSN=… WEIXIN_SDK_PATH=… \
      python3 scripts/wx_alias_check.py --sample 10
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from hippo.storage import PostgresStorage
from hippo.weixin_source import WeixinSource


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--pg-dsn', default=os.environ.get('HIPPO_PG_DSN'))
    p.add_argument('--sample', type=int, default=10, help='随机抽查多少个账号')
    p.add_argument('--all', action='store_true', help='跑全部（小心：每个账号一次列表请求）')
    p.add_argument('--sleep', type=float, default=2.0, help='账号间休眠秒数（默认 2）')
    p.add_argument('--out', default=None, help='失败名单写到这个文件')
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    if not args.pg_dsn:
        print('缺少 HIPPO_PG_DSN / --pg-dsn', file=sys.stderr)
        return 2
    storage = PostgresStorage(args.pg_dsn)
    query = (
        "select biz, nickname, coalesce(alias, '') from accounts where not is_disabled "
        'order by biz'
    )
    params: tuple = ()
    if not args.all:
        query = (
            "select biz, nickname, coalesce(alias, '') from accounts where not is_disabled "
            'order by random() limit %s'
        )
        params = (args.sample,)
    with storage.conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    ok = 0
    empty_alias = 0
    failed: list[tuple[str, str, str]] = []
    async with WeixinSource() as source:
        for index, (biz, nickname, alias) in enumerate(rows):
            if index:
                await asyncio.sleep(args.sleep)
            if not alias.strip():
                empty_alias += 1
                failed.append((nickname, '', 'alias 为空'))
                continue
            try:
                items = await source.list_articles(alias.strip(), biz, pages=1)
            except Exception as exc:
                failed.append((nickname, alias, str(exc)[:120]))
                continue
            if items:
                ok += 1
            else:
                failed.append((nickname, alias, '列表返回 0 篇'))
    storage.close()

    print(f'账号 {len(rows)} 个：成功 {ok}，alias 为空 {empty_alias}，失败 {len(failed) - empty_alias}')
    for nickname, alias, reason in failed[:20]:
        print(f'  ✗ {nickname}（{alias}）：{reason}')
    if args.out and failed:
        with open(args.out, 'w', encoding='utf-8') as fh:
            for nickname, alias, reason in failed:
                fh.write(f'{nickname}\t{alias}\t{reason}\n')
        print(f'完整失败名单 → {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
