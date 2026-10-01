"""把长链换成永久短链。

微信公众号的长链（`/s?__biz=…&mid=…&sn=…`、老式 `/mp/appmsg/show?appmsgid=…`）会失效，
短链（`/s/<slug>`）是永久的，所以库里的 `articles.link` 尽量统一成短链。

换法只能走协议：daemon 抓一次正文，响应里的 `short_link` 就是永久短链
（直接 HTTP 跟随 302 会被踢到验证码页）。daemon 侧有按篇计费的闸门，所以这个脚本很慢，
默认只处理最近的一批，用 `--shape` 圈定范围。

用法：
    HIPPO_PG_DSN=… WEIXIN_SDK_PATH=… .venv/bin/python3 scripts/wx_convert_links.py --shape mid --limit 100
    … --shape old --limit 1000        # 老式 /mp/appmsg/show，成功率和速度都差得多
    … --shape all                      # 全量（约 1 万篇，按闸门要跑很久）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import psycopg
from psycopg.rows import dict_row

# 与 daemon [articles].batch_size 对齐
BATCH = 5
SHAPES = {
    'mid': "link LIKE '%%/s?__biz=%%'",
    'old': "link LIKE '%%/mp/appmsg/show%%'",
    'all': "(link LIKE '%%/s?__biz=%%' OR link LIKE '%%/mp/appmsg/show%%')",
}


def log(message: str) -> None:
    print(f'[convert] {message}', flush=True)


def pick(dsn: str, shape: str, limit: int | None, retry_failed: bool) -> list[dict]:
    clause = SHAPES[shape]
    skip = '' if retry_failed else "AND NOT EXISTS (SELECT 1 FROM meta m WHERE m.key = 'link_convert_failed:' || a.id)"
    sql = f'SELECT a.id, a.link FROM articles a WHERE {clause} {skip} ORDER BY a.id DESC'
    if limit:
        sql += f' LIMIT {int(limit)}'
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return list(cur.fetchall())


def mark(dsn: str, article_pk: int, note: str) -> None:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            'INSERT INTO meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value',
            (f'link_convert_failed:{article_pk}', note[:200]),
        )
        conn.commit()


def update_link(dsn: str, article_pk: int, short_link: str) -> None:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute('UPDATE articles SET link = %s, updated_at = NOW() WHERE id = %s', (short_link, article_pk))
        conn.commit()


async def convert(dsn: str, shape: str, limit: int | None, retry_failed: bool, dry_run: bool) -> int:
    rows = pick(dsn, shape, limit, retry_failed)
    if not rows:
        log('没有待置换的长链')
        return 0
    log(f'待置换 {len(rows)} 篇（shape={shape}）')
    if dry_run:
        for row in rows[:5]:
            log(f'DRY-RUN {row["id"]} {row["link"][:80]}')
        return len(rows)

    sys.path.insert(0, os.environ['WEIXIN_SDK_PATH'])
    from weixin_bot import WeChatBot

    ok = failed = 0
    async with WeChatBot(host='127.0.0.1', port=9099) as bot:
        for start in range(0, len(rows), BATCH):
            chunk = rows[start : start + BATCH]
            urls = [str(r['link']) for r in chunk]
            try:
                bodies = await bot.get_article_bodies(urls, timeout=900.0)
            except Exception as exc:
                log(f'批次失败：{str(exc)[:100]}')
                for row in chunk:
                    mark(dsn, int(row['id']), f'batch error: {exc}')
                    failed += 1
                continue
            by_url = {b.url: b for b in bodies}
            for row in chunk:
                body = by_url.get(str(row['link']))
                short = getattr(body, 'short_link', None) if body else None
                if short and '/s/' in short:
                    update_link(dsn, int(row['id']), short)
                    ok += 1
                else:
                    mark(dsn, int(row['id']), '响应里没有 short_link')
                    failed += 1
            done = min(start + BATCH, len(rows))
            log(f'进度 {done}/{len(rows)}，换好 {ok}，失败 {failed}')
    log(f'完成：换好 {ok} 篇，失败 {failed} 篇')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='长链 → 永久短链')
    parser.add_argument('--pg-dsn', default=os.environ.get('HIPPO_PG_DSN', ''))
    parser.add_argument('--shape', choices=sorted(SHAPES), default='mid')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not args.pg_dsn:
        log('缺少 HIPPO_PG_DSN / --pg-dsn')
        return 2
    return asyncio.run(convert(args.pg_dsn, args.shape, args.limit, args.retry_failed, args.dry_run))


if __name__ == '__main__':
    raise SystemExit(main())
