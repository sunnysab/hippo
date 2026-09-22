#!/usr/bin/env python3
"""把 article_content.clean_html 搬到 article_document.raw_html。

线上正文表只保留派生内容（content_markdown + content_json）；原始 HTML 进
article_document，解析逻辑出问题时可以从原文重放。

用法::

    python3 scripts/migrate_clean_html.py --dry-run
    python3 scripts/migrate_clean_html.py --limit 2000        # 小批量验证
    python3 scripts/migrate_clean_html.py                     # 全量（可中断续跑）
    python3 scripts/migrate_clean_html.py --purge-source      # 搬完置空来源列

进度记在 meta 表 `migrate_clean_html:cursor`（上一个已搬的 article_pk），
中断后重跑会从断点继续；`--restart` 忽略游标从头来。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import psycopg

CURSOR_KEY = 'migrate_clean_html:cursor'
SOURCE = 'rendered'


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument('--pg-dsn', default=os.environ.get('HIPPO_PG_DSN'), help='默认取 HIPPO_PG_DSN')
    p.add_argument('--batch-size', type=int, default=2000, help='每批行数（默认 2000）')
    p.add_argument('--limit', type=int, default=None, help='本次最多搬多少行')
    p.add_argument('--dry-run', action='store_true', help='只统计，不写库')
    p.add_argument('--purge-source', action='store_true', help='搬完把来源列置空（谨慎）')
    p.add_argument('--restart', action='store_true', help='忽略 meta 游标，从头开始')
    p.add_argument('--sleep-ms', type=int, default=0, help='批间休眠毫秒（降低线上写入压力）')
    return p.parse_args()


def log(message: str) -> None:
    print(f'[migrate clean_html] {message}', file=sys.stderr, flush=True)


def read_cursor(conn) -> int:
    with conn.cursor() as cur:
        cur.execute('select value from meta where key = %s', (CURSOR_KEY,))
        row = cur.fetchone()
    return int(row[0]) if row and row[0] else 0


def save_cursor(conn, value: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            'insert into meta(key, value) values (%s, %s) '
            'on conflict (key) do update set value = excluded.value',
            (CURSOR_KEY, str(value)),
        )


def pending_count(conn, cap: int = 100_000) -> int:
    """待搬行数。全表 count 在 41 GB 的表上太慢，所以只数到 `cap` 就够判断。"""
    with conn.cursor() as cur:
        cur.execute(
            'select count(*) from (select 1 from article_content '
            "where clean_html is not null and btrim(clean_html) <> '' limit %s) t",
            (cap,),
        )
        return int(cur.fetchone()[0])


def migrated_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from article_document where source = %s and raw_html is not null",
            (SOURCE,),
        )
        return int(cur.fetchone()[0])


def migrate_batch(conn, cursor: int, batch_size: int, purge_source: bool) -> tuple[int, int]:
    """搬一批，返回 (本批行数, 新游标)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            with batch as (
                select c.article_pk, c.url_token, c.clean_html, c.updated_at
                  from article_content c
                 where c.article_pk > %s
                   and c.clean_html is not null
                   and btrim(c.clean_html) <> ''
                 order by c.article_pk
                 limit %s
            )
            insert into article_document
                (article_pk, source, url_token, raw_html, fetched_at, created_at, updated_at)
            select b.article_pk, %s, b.url_token, b.clean_html, b.updated_at, now(), now()
              from batch b
            on conflict (article_pk) do update
               set raw_html = excluded.raw_html,
                   url_token = coalesce(excluded.url_token, article_document.url_token),
                   source = excluded.source,
                   updated_at = now()
            returning article_pk
            """,
            (cursor, batch_size, SOURCE),
        )
        moved = [int(row[0]) for row in cur.fetchall() or []]
    if not moved:
        return 0, cursor
    if purge_source:
        with conn.cursor() as cur:
            cur.execute(
                'update article_content set clean_html = null where article_pk = any(%s)',
                (moved,),
            )
    return len(moved), max(moved)


def main() -> int:
    args = parse_args()
    if not args.pg_dsn:
        log('缺少数据库 DSN：请设置 HIPPO_PG_DSN 或传 --pg-dsn')
        return 2
    if args.batch_size <= 0:
        log('--batch-size 必须为正')
        return 2

    with psycopg.connect(args.pg_dsn) as conn:
        pending = pending_count(conn)
        done = migrated_count(conn)
        log(f'待搬 {pending} 行；article_document 已有 {done} 行')
        if args.dry_run:
            log('dry-run，未写库')
            return 0
        if pending == 0:
            log('没有待搬数据')
            return 0

        cursor = 0 if args.restart else read_cursor(conn)
        if cursor:
            log(f'从游标 article_pk > {cursor} 继续')

        total = 0
        started = time.monotonic()
        while True:
            if args.limit is not None and total >= args.limit:
                break
            batch = args.batch_size
            if args.limit is not None:
                batch = min(batch, args.limit - total)
            moved, cursor = migrate_batch(conn, cursor, batch, args.purge_source)
            conn.commit()
            if moved == 0:
                break
            total += moved
            save_cursor(conn, cursor)
            conn.commit()
            elapsed = time.monotonic() - started
            rate = total / elapsed if elapsed > 0 else 0
            log(f'已搬 {total} 行（游标 {cursor}，{rate:.0f} 行/秒）')
            if args.sleep_ms > 0:
                time.sleep(args.sleep_ms / 1000)

        elapsed = time.monotonic() - started
        log(f'完成：本批共 {total} 行，耗时 {elapsed:.0f}s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
