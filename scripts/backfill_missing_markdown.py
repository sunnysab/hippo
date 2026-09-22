#!/usr/bin/env python3
"""给「有原始 HTML、但没有 markdown」的文章补上派生内容。

`clean_html` 已从 article_content 拆到 article_document.raw_html；当年那批只写了
clean_html、没写 content_markdown 的文章（约 5000 篇）在检索里会退化成标题+摘要。
这里从 raw_html 重新渲染 markdown 与 blocks，只更新派生字段，**不碰**
article_images（重建 image 行会丢掉已有的 s3_key）。

用法::

    HIPPO_PG_DSN=… python3 scripts/backfill_missing_markdown.py --limit 50   # 小批量
    HIPPO_PG_DSN=… python3 scripts/backfill_missing_markdown.py              # 全量（可续跑）
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

CANDIDATE_SQL = """
SELECT c.article_pk, d.raw_html
  FROM article_content c
  JOIN article_document d ON d.article_pk = c.article_pk
 WHERE (c.content_markdown IS NULL OR btrim(c.content_markdown) = '')
   AND d.raw_html IS NOT NULL
   AND btrim(d.raw_html) <> ''
 ORDER BY c.article_pk
 LIMIT %s
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--pg-dsn', default=os.environ.get('HIPPO_PG_DSN'))
    p.add_argument('--batch-size', type=int, default=200)
    p.add_argument('--limit', type=int, default=None, help='本次最多处理多少篇')
    p.add_argument('--dry-run', action='store_true')
    return p.parse_args()


def log(message: str) -> None:
    print(f'[backfill md] {message}', file=sys.stderr, flush=True)


def main() -> int:
    args = parse_args()
    if not args.pg_dsn:
        log('缺少 HIPPO_PG_DSN / --pg-dsn')
        return 2

    storage = PostgresStorage(args.pg_dsn)
    done = failed = 0
    while args.limit is None or done + failed < args.limit:
        batch = args.batch_size
        if args.limit is not None:
            batch = min(batch, args.limit - done - failed)
        if batch <= 0:
            break
        with storage.conn.cursor() as cur:
            cur.execute(CANDIDATE_SQL, (batch,))
            rows = cur.fetchall()
        storage.rollback()
        if not rows:
            break

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
