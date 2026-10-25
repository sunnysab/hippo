"""Fix HTML entities left in the article text fields.

抓取时残留的 HTML 实体只污染纯文本字段：`title` 里的 `&quot;` / `&amp;` 之类应当还
原成字符。`unescape_text` 会对**含原始标签**的片段（digest 里常见的
`<a href="…&amp;mid=…">`）原样放过，那里的 `&amp;` 是合法转义，unescape 会把属性里
的 URL 写坏。

Usage:
    python scripts/fix_html_entities.py            # dry-run (report only)
    python scripts/fix_html_entities.py --execute  # apply fixes
"""

from __future__ import annotations

import argparse
import asyncio

from hippo.storage import open_storage
from hippo.utils import unescape_text

ENTITY_PATTERN = r'&(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);'

SQL_FIND = f"""
SELECT id, title, author, digest
FROM articles
WHERE title ~ '{ENTITY_PATTERN}'
   OR author ~ '{ENTITY_PATTERN}'
   OR digest ~ '{ENTITY_PATTERN}'
ORDER BY id
"""

SQL_UPDATE = """
UPDATE articles SET
    title = %s,
    author = %s,
    digest = %s,
    updated_at = now()
WHERE id = %s
"""


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

            updates: list[tuple[str, str | None, str | None, int]] = []

            for pk, title, author, digest in rows:
                new_title = unescape_text(title)
                new_author = unescape_text(author)
                new_digest = unescape_text(digest)

                changed = False

                for field, old, new in (
                    ('title', title, new_title),
                    ('author', author, new_author),
                    ('digest', digest, new_digest),
                ):
                    if new != old:
                        print(f'  id={pk} {field}: {old!r}')
                        print(f'             ->  {new!r}')
                        changed = True

                if changed:
                    print()
                    updates.append((new_title, new_author, new_digest, pk))

            if not updates:
                print('No changes needed (all entities already match unescaped form).')
                return

            if dry_run:
                print(f'DRY RUN: {len(updates)} row(s) would be updated. Run with --execute to apply.')
                return

            for new_title, new_author, new_digest, pk in updates:
                await cur.execute(SQL_UPDATE, (new_title, new_author, new_digest, pk))

            await conn.commit()
            print(f'Fixed {len(updates)} row(s).')


def main() -> None:
    parser = argparse.ArgumentParser(description='Fix HTML entities in article fields')
    parser.add_argument('--execute', action='store_true', help='Apply fixes (default: dry-run)')
    args = parser.parse_args()
    asyncio.run(run(dry_run=not args.execute))


if __name__ == '__main__':
    main()
