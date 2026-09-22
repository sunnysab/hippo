#!/usr/bin/env python3
"""从 article_document.raw_html 重放派生内容（markdown + blocks）。

原始 HTML 从 article_content 拆出来存进 article_document 就是为了这个：渲染逻辑
改进、或者某批数据渲染坏了，都能从原文重放，不用重新去微信拉。

三种用法：

* 补缺（默认）：只处理 `content_markdown` 为空的（历史上那批只写了 HTML 的文章）；
* `--biz` / `--slug`：重放某个号的全部文章，或指定一篇（修一篇坏数据）；
* `--force`：连已有 markdown 的一起重放（换渲染逻辑后全量刷一遍）。

只更新派生字段（`content_markdown` + `content_json`），**不碰** article_images
—— 重建 image 行会丢掉已经上传好的 s3_key，图片块按 `orig_url` 重新关联即可。

用法::

    HIPPO_PG_DSN=… python3 scripts/replay_article_documents.py --limit 50      # 小批量
    HIPPO_PG_DSN=… python3 scripts/replay_article_documents.py                 # 补完全部缺口
    HIPPO_PG_DSN=… python3 scripts/replay_article_documents.py --biz MjM5… --slug AbCdEf
    HIPPO_PG_DSN=… python3 scripts/replay_article_documents.py --force --biz MjM5…
    HIPPO_PG_DSN=… python3 scripts/replay_article_documents.py --check         # 只校验不写
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from markdownify import markdownify

from hippo.downloader import _attach_image_block_metadata, _parse_markdown_blocks
from hippo.storage import PostgresStorage
from hippo.wechat_parser import _postprocess_markdown

BASE_SQL = """
SELECT c.article_pk, d.raw_html
  FROM article_content c
  JOIN article_document d ON d.article_pk = c.article_pk
  JOIN articles a ON a.id = c.article_pk
 WHERE d.raw_html IS NOT NULL
   AND btrim(d.raw_html) <> ''
   AND {filters}
   AND c.article_pk > %s
 ORDER BY c.article_pk
 LIMIT %s
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--pg-dsn', default=os.environ.get('HIPPO_PG_DSN'))
    p.add_argument('--batch-size', type=int, default=200)
    p.add_argument('--limit', type=int, default=None, help='本次最多处理多少篇')
    p.add_argument('--biz', default=None, help='只重放这个账号（accounts.biz）')
    p.add_argument('--slug', default=None, help='只重放这一篇（文章短链 slug，需配合 --biz）')
    p.add_argument('--force', action='store_true', help='连已有 markdown 的一起重放')
    p.add_argument('--check', action='store_true', help='只校验 raw_html 能否渲染出内容，不写库')
    p.add_argument('--dry-run', action='store_true')
    return p.parse_args()


def log(message: str) -> None:
    print(f'[replay] {message}', file=sys.stderr, flush=True)


def main() -> int:
    args = parse_args()
    if not args.pg_dsn:
        log('缺少 HIPPO_PG_DSN / --pg-dsn')
        return 2

    filters = []
    params: list = []
    if not args.force:
        filters.append("(c.content_markdown IS NULL OR btrim(c.content_markdown) = '')")
    if args.biz:
        filters.append('a.biz = %s')
        params.append(args.biz)
    if args.slug:
        filters.append("a.link LIKE '%%/s/' || %s")
        params.append(args.slug)
    where = ' AND '.join(filters) if filters else 'TRUE'

    storage = PostgresStorage(args.pg_dsn)
    done = failed = 0
    cursor = 0
    while args.limit is None or done + failed < args.limit:
        batch = args.batch_size
        if args.limit is not None:
            batch = min(batch, args.limit - done - failed)
        if batch <= 0:
            break
        query = BASE_SQL.format(filters=where)
        with storage.conn.cursor() as cur:
            cur.execute(query, (*params, cursor, batch))
            rows = cur.fetchall()
        storage.rollback()
        if not rows:
            break
        cursor = int(rows[-1][0])

        article_pks = [int(row[0]) for row in rows]
        with storage.conn.cursor() as cur:
            cur.execute(
                'SELECT article_pk, id, orig_url FROM article_images WHERE article_pk = ANY(%s)',
                (article_pks,),
            )
            image_rows = cur.fetchall()
        storage.rollback()
        image_ids: dict[int, dict[str, int]] = {}
        for article_pk, image_id, orig_url in image_rows:
            image_ids.setdefault(int(article_pk), {})[str(orig_url)] = int(image_id)

        updates: list[tuple[str, str, int]] = []
        for article_pk, raw_html in rows:
            try:
                markdown = _postprocess_markdown(markdownify(raw_html, heading_style='ATX'))
                _title, _cover, blocks, body_markdown = _parse_markdown_blocks(markdown)
                blocks = _attach_image_block_metadata(
                    blocks,
                    resolve_url=lambda value: value.strip() if isinstance(value, str) else None,
                    image_id_by_url=image_ids.get(int(article_pk)),
                )
                updates.append((body_markdown, json.dumps(blocks, ensure_ascii=False), int(article_pk)))
            except Exception as exc:
                log(f'✗ 文章 {article_pk} 渲染失败：{exc}')
                failed += 1

        if args.check:
            empty = len(rows) - len(updates)
            log(f'check：本批 {len(rows)} 篇，可渲染 {len(updates)}，渲染为空 {empty}')
            done += len(updates)
            failed += empty
            if args.limit is not None:
                break
            continue
        if args.dry_run:
            log(f'dry-run：本批可更新 {len(updates)} 篇')
            done += len(updates)
            break
        if updates:
            with storage.transaction(), storage.conn.cursor() as cur:
                cur.executemany(
                    'UPDATE article_content SET content_markdown = %s, content_json = %s::jsonb, '
                    'updated_at = NOW() WHERE article_pk = %s',
                    updates,
                )
        done += len(updates)
        log(f'已补 {done} 篇（失败 {failed}）')

    storage.close()
    log(f'完成：补 {done} 篇，失败 {failed} 篇')
    return 0


if __name__ == '__main__':
    sys.exit(main())
