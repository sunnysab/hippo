"""Fix HTML entities in article title/author/digest fields.

Usage:
    python scripts/fix_html_entities.py            # dry-run (report only)
    python scripts/fix_html_entities.py --execute  # apply fixes
"""

from __future__ import annotations

import argparse
import html

from hippo.storage import open_storage


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


def run(dry_run: bool = True) -> None:
    storage = open_storage(auto_init=False)
    conn = storage.conn

    with conn.cursor() as cur:
        cur.execute(SQL_FIND)
        rows = cur.fetchall()

        if not rows:
            print("No rows with HTML entities found.")
            return

        print(f"Found {len(rows)} row(s) with HTML entities:\n")

        updates: list[tuple[str, str | None, str | None, int]] = []

        for row in rows:
            pk, title, author, digest = row
            new_title = full_unescape(title) if needs_fix(title) else title
            new_author = full_unescape(author) if needs_fix(author) else author
            new_digest = full_unescape(digest) if needs_fix(digest) else digest

            changed = False

            if new_title != title:
                print(f"  id={pk} title:  {title!r}")
                print(f"             ->  {new_title!r}")
                changed = True
            if new_author != author:
                print(f"  id={pk} author: {author!r}")
                print(f"             ->  {new_author!r}")
                changed = True
            if new_digest != digest:
                print(f"  id={pk} digest: {digest!r}")
                print(f"             ->  {new_digest!r}")
                changed = True

            if changed:
                print()
                updates.append((new_title, new_author, new_digest, pk))

        if not updates:
            print("No changes needed (all entities already match unescaped form).")
            return

        if dry_run:
            print(f"DRY RUN: {len(updates)} row(s) would be updated. Run with --execute to apply.")
            return

        for new_title, new_author, new_digest, pk in updates:
            cur.execute(SQL_UPDATE, (new_title, new_author, new_digest, pk))

        conn.commit()
        print(f"Fixed {len(updates)} row(s).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fix HTML entities in article fields")
    parser.add_argument("--execute", action="store_true", help="Apply fixes (default: dry-run)")
    args = parser.parse_args()
    run(dry_run=not args.execute)


if __name__ == "__main__":
    main()
