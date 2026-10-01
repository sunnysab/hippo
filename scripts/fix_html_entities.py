"""Fix HTML entities left in the article title/author fields.

抓取时残留的 HTML 实体只污染纯文本字段：`title` 里的 `&quot;` / `&amp;` 之类应当还
原成字符。`digest` **不处理** —— 它存的是 HTML 片段（`<a href="…&amp;mid=…">`），
那里的 `&amp;` 是合法转义，unescape 会把属性里的 URL 写坏。

Usage:
    python scripts/fix_html_entities.py            # dry-run (report only)
    python scripts/fix_html_entities.py --execute  # apply fixes
"""

from __future__ import annotations

import argparse
import asyncio
import html

from hippo.storage import open_storage

ENTITY_PATTERN = r'&(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);'

SQL_FIND = f"""
SELECT id, title, author
FROM articles
WHERE title ~ '{ENTITY_PATTERN}'
   OR author ~ '{ENTITY_PATTERN}'
ORDER BY id
"""

SQL_UPDATE = """
UPDATE articles SET
    title = %s,
    author = %s,
    updated_at = now()
WHERE id = %s
"""


def full_unescape(value: str | None) -> str:
    if not value or '&' not in value:
        return value or ''
    prev = value
    while True:
        unescaped = html.unescape(prev)
        if unescaped == prev:
            return unescaped
        prev = unescaped


def needs_fix(value: str | None) -> bool:
    if not value:
        return False
    return '&' in value and value != full_unescape(value)


async def run(dry_run: bool = True) -> None:
    async with open_storage(auto_init=False) as storage:
        conn = storage.conn

        async with conn.cursor() as cur:
            await cur.execute(SQL_FIND)
            rows = await cur.fetchall()

            if not rows:
                print('No rows with HTML entities found.')
                return

            print(f'Found {len(rows)} row(s) with HTML entities:\n')

            updates: list[tuple[str, str | None, int]] = []

            for pk, title, author in rows:
                new_title = full_unescape(title) if needs_fix(title) else title
                new_author = full_unescape(author) if needs_fix(author) else author

                changed = False

                if new_title != title:
                    print(f'  id={pk} title:  {title!r}')
                    print(f'             ->  {new_title!r}')
                    changed = True
                if new_author != author:
                    print(f'  id={pk} author: {author!r}')
                    print(f'             ->  {new_author!r}')
                    changed = True

                if changed:
                    print()
                    updates.append((new_title, new_author, pk))

            if not updates:
                print('No changes needed (all entities already match unescaped form).')
                return

            if dry_run:
                print(f'DRY RUN: {len(updates)} row(s) would be updated. Run with --execute to apply.')
                return

            for new_title, new_author, pk in updates:
                await cur.execute(SQL_UPDATE, (new_title, new_author, pk))

            await conn.commit()
            print(f'Fixed {len(updates)} row(s).')


def main() -> None:
    parser = argparse.ArgumentParser(description='Fix HTML entities in article fields')
    parser.add_argument('--execute', action='store_true', help='Apply fixes (default: dry-run)')
    args = parser.parse_args()
    asyncio.run(run(dry_run=not args.execute))


if __name__ == '__main__':
    main()
