#!/usr/bin/env python3
"""从 article_document.raw_html 重放派生内容（markdown + blocks）。

原始 HTML 从 article_content 拆出来存进 article_document 就是为了这个：渲染逻辑
改进、或者某批数据渲染坏了，都能从原文重放，不用重新去微信拉。

三种用法：

* 补缺（默认）：只处理 `content_markdown` 为空的（历史上那批只写了 HTML 的文章）；
* `--biz` / `--slug` / `--since`：重放某个号的全部文章、指定一篇，或某个时间之后同步的；
* `--force`：连已有 markdown 的一起重放（换渲染逻辑后全量刷一遍）。

默认只更新派生字段（`content_markdown` + `content_json`），**不碰** article_images
—— 重建 image 行会丢掉已经上传好的 s3_key，图片块按 `orig_url` 重新关联即可。

`--repair-images` 是给正文丢图那类事故用的：图片块里出现了 `article_images` 里没有的
URL（历史上正文渲染漏掉的图）时补一行待下载的记录，并删掉 URL 已经不成立的废行（例如
曾经把 Markdown 的 title 当成 URL 写进去的 `https://mp.weixin.qq.com/s/"1.png"`）。
配合 `hippo article backfill-images` 把这些图抓回来。

用法::

    HIPPO_PG_DSN=… python3 scripts/replay_article_documents.py --limit 50      # 小批量
    HIPPO_PG_DSN=… python3 scripts/replay_article_documents.py                 # 补完全部缺口
    HIPPO_PG_DSN=… python3 scripts/replay_article_documents.py --biz MjM5… --slug AbCdEf
    HIPPO_PG_DSN=… python3 scripts/replay_article_documents.py --force --biz MjM5…
    HIPPO_PG_DSN=… python3 scripts/replay_article_documents.py --check         # 只校验不写
    HIPPO_PG_DSN=… python3 scripts/replay_article_documents.py --force --since 2026-09-20 \
        --repair-images --dry-run                                             # 先看量
    HIPPO_PG_DSN=… python3 scripts/replay_article_documents.py --force --since 2026-09-20 \
        --repair-images                                                       # 再写
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from markdownify import markdownify

from hippo.downloader import _attach_image_block_metadata, parse_markdown_blocks
from hippo.normalize_html import promote_lazy_image_srcs
from hippo.storage import PostgresStorage
from hippo.wechat_parser import _postprocess_markdown

BASE_SQL = """
SELECT c.article_pk, d.raw_html, c.content_markdown, c.content_json::text
  FROM article_content c
  JOIN article_document d ON d.article_pk = c.article_pk
  JOIN articles a ON a.id = c.article_pk
 WHERE d.raw_html IS NOT NULL
   AND btrim(d.raw_html) <> ''
   AND {filters}
   AND NOT EXISTS (
       SELECT 1 FROM article_download_attempts t
        WHERE t.biz = a.biz AND t.article_id = a.article_id AND NOT t.retryable
   )
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
    p.add_argument('--since', default=None, help='只重放这个日期（YYYY-MM-DD）之后同步的文章')
    p.add_argument(
        '--repair-images',
        action='store_true',
        help='补上正文引用但 article_images 里没有的图片行，并清理 URL 已失效的废行',
    )
    p.add_argument('--check', action='store_true', help='只校验 raw_html 能否渲染出内容，不写库')
    p.add_argument('--dry-run', action='store_true')
    return p.parse_args()


async def mark_skipped(storage: PostgresStorage, article_pk: int, reason: str) -> None:
    """记一笔不可重试的尝试，好让候选查询下次跳过它（否则会反复取到同一批）。"""
    async with storage.transaction(), storage.conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO article_download_attempts
                (biz, article_id, attempts, last_error, last_attempt_at, created_at, error_type, retryable)
            SELECT a.biz, a.article_id, 1, %s, NOW(), NOW(), 'replay_empty', FALSE
              FROM articles a WHERE a.id = %s
            ON CONFLICT (biz, article_id) DO UPDATE SET
                attempts = article_download_attempts.attempts + 1,
                last_error = EXCLUDED.last_error,
                last_attempt_at = NOW(),
                retryable = FALSE
            """,
            (reason, article_pk),
        )


async def _keep_url(value: object) -> str | None:
    """图片块里的 URL 在正文里就已经是绝对的，这里只做去空白。"""
    return value.strip() if isinstance(value, str) else None


def _needs_repair(
    blocks: list[dict],
    body_markdown: str,
    stored_markdown: str | None,
    stored_json: str | None,
) -> bool:
    """这次渲染的结果与库里的是否一致。一致就不必再写一遍。

    ``jsonb`` 会自己重排 key、去掉空白，所以比的是解析后的结构而不是字符串。
    """
    if (stored_markdown or '') != body_markdown:
        return True
    try:
        stored = json.loads(stored_json or 'null')
    except json.JSONDecodeError:
        return True
    return stored != blocks


def _is_dead_image_url(value: str) -> bool:
    """URL 已经不可能指向图片的行：历史上把 Markdown 的 title 当成了 URL。

    形如 `https://mp.weixin.qq.com/s/"1.png"` —— 引号只可能来自 Markdown 的
    图片标题，不可能是真实图片地址。
    """
    return '/"' in value or value.endswith('"')


def log(message: str) -> None:
    print(f'[replay] {message}', file=sys.stderr, flush=True)


async def main() -> int:
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
    if args.since:
        filters.append('a.created_at >= %s::date')
        params.append(args.since)
    where = ' AND '.join(filters) if filters else 'TRUE'

    done = failed = skipped = unchanged = 0
    async with PostgresStorage(args.pg_dsn) as storage:
        cursor = 0
        while args.limit is None or done + failed < args.limit:
            batch = args.batch_size
            if args.limit is not None:
                batch = min(batch, args.limit - done - failed)
            if batch <= 0:
                break
            query = BASE_SQL.format(filters=where)
            async with storage.conn.cursor() as cur:
                await cur.execute(query, (*params, cursor, batch))
                rows = await cur.fetchall()
            await storage.rollback()
            if not rows:
                break
            cursor = int(rows[-1][0])

            article_pks = [int(row[0]) for row in rows]
            async with storage.conn.cursor() as cur:
                await cur.execute(
                    'SELECT article_pk, id, orig_url FROM article_images WHERE article_pk = ANY(%s)',
                    (article_pks,),
                )
                image_rows = await cur.fetchall()
            await storage.rollback()
            image_ids: dict[int, dict[str, int]] = {}
            stale_image_ids: list[int] = []
            for article_pk, image_id, orig_url in image_rows:
                if args.repair_images and _is_dead_image_url(str(orig_url or '')):
                    stale_image_ids.append(int(image_id))
                    continue
                image_ids.setdefault(int(article_pk), {})[str(orig_url)] = int(image_id)

            # 先把正文渲染出来（不带图片 id），好知道缺哪些图
            parsed: list[tuple[int, str, list[dict], str]] = []
            pending_images: list[tuple[int, int, str]] = []
            # 同一篇里同一张图可能出现多次（分隔图、二维码），行只建一条。
            seen_pending: set[tuple[int, str]] = set()
            for article_pk, raw_html, stored_markdown, stored_json in rows:
                article_pk = int(article_pk)
                try:
                    markdown = _postprocess_markdown(
                        markdownify(promote_lazy_image_srcs(raw_html), heading_style='ATX')
                    )
                    _title, _cover, blocks, body_markdown = parse_markdown_blocks(markdown)
                except Exception as exc:
                    log(f'✗ 文章 {article_pk} 渲染失败：{exc}')
                    failed += 1
                    continue
                if args.repair_images:
                    # 先用库里已有的图片行关联一遍，再判断这篇到底要不要动：
                    # 只有「渲染结果变了」或者「正文引用的图没有对应行」才值得写。
                    known = image_ids.setdefault(article_pk, {})
                    attached = await _attach_image_block_metadata(
                        blocks, resolve_url=_keep_url, image_id_by_url=known
                    )
                    if not _needs_repair(attached, body_markdown, stored_markdown, stored_json):
                        unchanged += 1
                        continue
                    for position, block in enumerate(
                        block for block in attached if block.get('type') == 'image'
                    ):
                        url = block.get('orig_url')
                        if not url or block.get('image_id') is not None:
                            continue
                        key = (article_pk, str(url))
                        if key in seen_pending:
                            continue
                        seen_pending.add(key)
                        pending_images.append((article_pk, position, str(url)))
                    parsed.append((article_pk, body_markdown, blocks, raw_html))
                    continue
                parsed.append((article_pk, body_markdown, blocks, raw_html))

            # 缺的图补一行待下载记录（s3_key 为空，由 backfill-images 去抓）
            if pending_images and not (args.check or args.dry_run):
                async with storage.transaction(), storage.conn.cursor() as cur:
                    for article_pk, position, url in pending_images:
                        await cur.execute(
                            'INSERT INTO article_images (article_pk, position, kind, orig_url, updated_at)'
                            " VALUES (%s, %s, 'inline', %s, NOW()) RETURNING id",
                            (article_pk, position, url),
                        )
                        image_ids.setdefault(article_pk, {})[url] = int((await cur.fetchone())[0])

            updates: list[tuple[str, str, int]] = []
            for article_pk, body_markdown, blocks, _raw_html in parsed:
                try:
                    blocks = await _attach_image_block_metadata(
                        blocks,
                        resolve_url=_keep_url,
                        image_id_by_url=image_ids.get(article_pk),
                    )
                    updates.append((body_markdown, json.dumps(blocks, ensure_ascii=False), article_pk))
                except Exception as exc:
                    log(f'✗ 文章 {article_pk} 关联图片失败：{exc}')
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
                log(
                    f'dry-run：本批 {len(rows)} 篇里需要修 {len(updates)} 篇'
                    f'（已一致 {unchanged} 篇），待补图片行 {len(pending_images)} 条，'
                    f'待清理废行 {len(stale_image_ids)} 条'
                )
                done += len(updates)
                break
            if updates:
                async with storage.transaction(), storage.conn.cursor() as cur:
                    await cur.executemany(
                        'UPDATE article_content SET content_markdown = %s, content_json = %s::jsonb, '
                        'updated_at = NOW() WHERE article_pk = %s',
                        updates,
                    )
                    if stale_image_ids:
                        await cur.execute('DELETE FROM article_images WHERE id = ANY(%s)', (stale_image_ids,))
            done += len(updates)
            log(f'已补 {done} 篇（失败 {failed}）')

    log(f'完成：补 {done} 篇，跳过 {skipped} 篇（渲染不出文字），失败 {failed} 篇')
    if args.repair_images and not (args.check or args.dry_run):
        log('图片行已就绪，用 `hippo article backfill-images` 把缺的图抓回来')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
