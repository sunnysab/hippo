#!/usr/bin/env python3
"""用列表接口反推 gh_id：`get_biz_articles` 的返回值里就带着解析后的 gh_。

之前那批「无法解析」大多是 searchcontact 被限流造成的假失败（重试就通了），
而这个接口本来就会把 alias 解析成 gh_ 并把结果放在响应的 `biz` 字段里 ——
调一次列表既能拿到 gh_，也顺带把该号的新文章入队，比单独去解析划算。

用法::

    HIPPO_PG_DSN=… WEIXIN_SDK_PATH=… python3 scripts/wx_resolve_gh_by_list.py --limit 5
    HIPPO_PG_DSN=… WEIXIN_SDK_PATH=… python3 scripts/wx_resolve_gh_by_list.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from hippo.storage import PostgresStorage
from hippo.weixin_source import load_bot_class

# 账号之间留足间隔：searchcontact 是会被熔断的窄口（一次密集调用能封它几十分钟）
ACCOUNT_INTERVAL = 15.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--pg-dsn', default=os.environ.get('HIPPO_PG_DSN'))
    p.add_argument('--limit', type=int, default=None, help='本次最多处理多少个账号')
    p.add_argument('--interval', type=float, default=ACCOUNT_INTERVAL, help='账号之间休眠秒数')
    return p.parse_args()


def log(message: str) -> None:
    print(f'[resolve gh] {message}', file=sys.stderr, flush=True)


async def main() -> int:
    args = parse_args()
    if not args.pg_dsn:
        log('缺少 HIPPO_PG_DSN / --pg-dsn')
        return 2

    storage = PostgresStorage(args.pg_dsn)
    # `gh_unresolvable:<biz>` 是人工确认过"这个 alias 本身无效"的标记，别再试
    query = (
        "SELECT biz, nickname, alias FROM accounts a "
        "WHERE gh_id IS NULL AND NOT is_disabled AND coalesce(alias, '') <> '' "
        "  AND NOT EXISTS (SELECT 1 FROM meta m WHERE m.key = 'gh_unresolvable:' || a.biz) "
        'ORDER BY article_count DESC NULLS LAST'
    )
    with storage.conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()
    storage.rollback()
    if args.limit is not None:
        rows = rows[: args.limit]
    log(f'待解析 {len(rows)} 个账号（用 alias 调一次列表）')

    ok = failed = 0
    bot_class = load_bot_class()
    bot = bot_class(
        host=os.environ.get('WEIXIN_DAEMON_HOST', '127.0.0.1'),
        port=int(os.environ.get('WEIXIN_DAEMON_PORT', '9099')),
    )
    async with bot:
        for index, (biz, nickname, alias) in enumerate(rows):
            if index:
                await asyncio.sleep(max(args.interval, 5.0))
            try:
                res = await bot.call('get_biz_articles', {'biz': str(alias), 'pages': 1})
            except Exception as exc:
                log(f'✗ {nickname}（{alias}）：{exc}')
                failed += 1
                continue
            gh_id = str((res or {}).get('biz') or '')
            count = int((res or {}).get('count') or 0)
            if gh_id.startswith('gh_'):
                with storage.transaction():
                    storage.accounts.set_gh_id(str(biz), gh_id)
                ok += 1
                log(f'+ {nickname}（{alias}）→ {gh_id}（列表 {count} 篇）')
            else:
                failed += 1
                log(f'✗ {nickname}（{alias}）：列表返回但没给 gh_（count={count}）')

    storage.close()
    log(f'完成：解析 {ok} 个，失败 {failed} 个')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
