"""把跟踪的公众号关注一遍。

关注之后微信才会把 `article_push` 推给机器人账号，也就是推送监听能覆盖到这些号。
已关注过的用 `meta` 里的 `followed:<gh_id>` 记着，重跑会跳过（断点续跑）。

用法：
    HIPPO_PG_DSN=... WEIXIN_SDK_PATH=... .venv/bin/python3 scripts/wx_follow_accounts.py
    ... --sleep 8 --limit 20 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row

FOLLOWED_PREFIX = 'followed:'


def log(message: str) -> None:
    print(f'[follow] {message}', flush=True)


def pending_accounts(dsn: str, limit: int | None) -> list[dict]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.nickname, a.gh_id
              FROM accounts a
             WHERE NOT a.is_disabled
               AND a.gh_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM meta m WHERE m.key = %s || a.gh_id)
             ORDER BY a.nickname
            """,
            (FOLLOWED_PREFIX,),
        )
        rows = list(cur.fetchall())
    return rows[:limit] if limit else rows


def mark_followed(dsn: str, gh_id: str, note: str) -> None:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (f'{FOLLOWED_PREFIX}{gh_id}', note),
        )
        conn.commit()


async def follow_all(dsn: str, *, sleep_seconds: float, limit: int | None, dry_run: bool) -> int:
    rows = pending_accounts(dsn, limit)
    if not rows:
        log('没有待关注的账号')
        return 0
    log(f'待关注 {len(rows)} 个，每个间隔 {sleep_seconds}s')
    if dry_run:
        for row in rows:
            log(f"DRY-RUN {row['nickname']} {row['gh_id']}")
        return len(rows)

    sys.path.insert(0, os.environ['WEIXIN_SDK_PATH'])
    from weixin_bot import WeChatBot

    ok = failed = 0
    async with WeChatBot(host='127.0.0.1', port=9099) as bot:
        for index, row in enumerate(rows, 1):
            gh_id = row['gh_id']
            nickname = row['nickname']
            try:
                res = await bot.call('follow_biz', {'gh_id': gh_id})
                if isinstance(res, dict) and res.get('followed'):
                    mark_followed(dsn, gh_id, f'{nickname} {datetime.now(UTC).isoformat()}')
                    ok += 1
                    log(f'{index}/{len(rows)} ✓ {nickname} ({gh_id})')
                else:
                    failed += 1
                    log(f'{index}/{len(rows)} ✗ {nickname}: {res}')
            except Exception as exc:
                failed += 1
                log(f'{index}/{len(rows)} ✗ {nickname}: {exc}')
            if index < len(rows):
                await asyncio.sleep(sleep_seconds)
    log(f'完成：成功 {ok}，失败 {failed}')
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description='关注所有跟踪的公众号')
    parser.add_argument('--pg-dsn', default=os.environ.get('HIPPO_PG_DSN', ''))
    parser.add_argument('--sleep', type=float, default=6.0, help='两个关注之间的间隔秒数')
    parser.add_argument('--limit', type=int, help='本次最多关注几个')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not args.pg_dsn:
        log('缺少 HIPPO_PG_DSN / --pg-dsn')
        return 2
    return asyncio.run(
        follow_all(args.pg_dsn, sleep_seconds=args.sleep, limit=args.limit, dry_run=args.dry_run)
    )


if __name__ == '__main__':
    raise SystemExit(main())
