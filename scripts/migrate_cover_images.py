"""Backfill cover images and store cover as article_images.id."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from hippo.storage import open_storage


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_numeric_cover(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        return value.strip().isdigit()
    return False


def _ensure_cover_image(cur, *, article_pk: int, cover_url: str, now: datetime) -> int:
    cur.execute(
        """
        SELECT id, kind
        FROM article_images
        WHERE article_pk = %s AND orig_url = %s
        LIMIT 1
        """,
        (article_pk, cover_url),
    )
    row = cur.fetchone()
    if row:
        image_id, kind = row[0], row[1]
        if kind != "cover":
            cur.execute(
                """
                UPDATE article_images
                SET kind = 'cover',
                    position = 0,
                    updated_at = %s
                WHERE id = %s
                """,
                (now, image_id),
            )
        return int(image_id)
    cur.execute(
        """
        SELECT id
        FROM article_images
        WHERE article_pk = %s AND kind = 'cover'
        ORDER BY id DESC
        LIMIT 1
        """,
        (article_pk,),
    )
    row = cur.fetchone()
    if row:
        image_id = int(row[0])
        cur.execute(
            """
            UPDATE article_images
            SET orig_url = %s,
                position = 0,
                content_type = NULL,
                s3_key = NULL,
                failed_at = NULL,
                failed_reason = NULL,
                updated_at = %s
            WHERE id = %s
            """,
            (cover_url, now, image_id),
        )
        return image_id
    cur.execute(
        """
        INSERT INTO article_images
            (article_pk, position, kind, orig_url, content_type, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (article_pk, 0, "cover", cover_url, None, now),
    )
    return int(cur.fetchone()[0])


def migrate(*, batch_size: int, alter_type: bool) -> None:
    last_id = 0
    total = 0
    with open_storage() as storage:
        while True:
            with storage.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, cover
                    FROM articles
                    WHERE cover IS NOT NULL
                      AND cover <> ''
                      AND id > %s
                    ORDER BY id ASC
                    LIMIT %s
                    """,
                    (last_id, batch_size),
                )
                rows = cur.fetchall()
            if not rows:
                break
            now = _utc_now()
            with storage.transaction():
                with storage.conn.cursor() as cur:
                    for article_pk, cover_url in rows:
                        if _is_numeric_cover(cover_url):
                            continue
                        cover_id = _ensure_cover_image(
                            cur,
                            article_pk=int(article_pk),
                            cover_url=str(cover_url),
                            now=now,
                        )
                        cur.execute(
                            "UPDATE articles SET cover = %s, updated_at = %s WHERE id = %s",
                            (cover_id, now, article_pk),
                        )
                        total += 1
            last_id = rows[-1][0]
        if alter_type:
            remaining = []
            last_id = 0
            while True:
                with storage.conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, cover
                        FROM articles
                        WHERE cover IS NOT NULL
                          AND cover <> ''
                          AND id > %s
                        ORDER BY id ASC
                        LIMIT %s
                        """,
                        (last_id, batch_size),
                    )
                    rows = cur.fetchall()
                if not rows:
                    break
                for article_pk, cover_url in rows:
                    if not _is_numeric_cover(cover_url):
                        remaining.append((article_pk, cover_url))
                        if len(remaining) >= 5:
                            break
                if remaining:
                    break
                last_id = rows[-1][0]
            if remaining:
                sample = ", ".join(str(item[0]) for item in remaining[:5])
                raise RuntimeError(
                    f"Non-numeric cover still exists. sample_article_ids=[{sample}]"
                )
            with storage.transaction():
                with storage.conn.cursor() as cur:
                    cur.execute(
                        """
                        ALTER TABLE articles
                        ALTER COLUMN cover TYPE INTEGER
                        USING NULLIF(cover, '')::integer
                        """
                    )
    print(f"migrated={total}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill cover images and store cover as image id.")
    parser.add_argument("--batch", type=int, default=200, help="Batch size")
    parser.add_argument("--alter-type", action="store_true", help="Alter articles.cover to INTEGER")
    args = parser.parse_args()
    migrate(batch_size=max(args.batch, 1), alter_type=bool(args.alter_type))


if __name__ == "__main__":
    main()
