#!/usr/bin/env python3
"""Export SQLite data to PostgreSQL."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

import psycopg2
import psycopg2.extras

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from wechatcli.config import DB_PATH
from wechatcli.storage import PostgresStorage


def chunked(iterable: Iterable, size: int):
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def export_meta(pg_conn, rows):
    with pg_conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO meta(key, value)
            VALUES %s
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            rows,
        )


def export_accounts(pg_conn, rows):
    with pg_conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO accounts (
                biz, nickname, alias, round_head_img, uin, key, pass_ticket,
                is_default, last_synced_at, created_at, updated_at
            )
            VALUES %s
            ON CONFLICT (biz) DO UPDATE SET
                nickname=EXCLUDED.nickname,
                alias=EXCLUDED.alias,
                round_head_img=EXCLUDED.round_head_img,
                uin=EXCLUDED.uin,
                key=EXCLUDED.key,
                pass_ticket=EXCLUDED.pass_ticket,
                is_default=EXCLUDED.is_default,
                last_synced_at=EXCLUDED.last_synced_at,
                updated_at=EXCLUDED.updated_at
            """,
            rows,
        )


def export_login_sessions(pg_conn, rows):
    with pg_conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO login_sessions (
                id, token, cookies_json, nickname, avatar, is_default, created_at, updated_at
            )
            VALUES %s
            ON CONFLICT (id) DO UPDATE SET
                token=EXCLUDED.token,
                cookies_json=EXCLUDED.cookies_json,
                nickname=EXCLUDED.nickname,
                avatar=EXCLUDED.avatar,
                is_default=EXCLUDED.is_default,
                updated_at=EXCLUDED.updated_at
            """,
            rows,
        )


def export_articles(pg_conn, rows):
    with pg_conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO articles (
                biz, article_id, title, author, digest, cover, link,
                source_url, publish_at, raw_json, created_at, updated_at
            )
            VALUES %s
            ON CONFLICT (biz, article_id) DO UPDATE SET
                title=EXCLUDED.title,
                author=EXCLUDED.author,
                digest=EXCLUDED.digest,
                cover=EXCLUDED.cover,
                link=EXCLUDED.link,
                source_url=EXCLUDED.source_url,
                publish_at=EXCLUDED.publish_at,
                raw_json=EXCLUDED.raw_json,
                updated_at=EXCLUDED.updated_at
            """,
            rows,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Export SQLite data to PostgreSQL")
    parser.add_argument(
        "--sqlite-path",
        default=str(DB_PATH),
        help="SQLite DB path (default: wechatcli config DB_PATH)",
    )
    parser.add_argument(
        "--pg-dsn",
        default=os.environ.get("WECHATCLI_PG_DSN", ""),
        help="PostgreSQL DSN (or set WECHATCLI_PG_DSN)",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="Truncate destination tables before export",
    )
    args = parser.parse_args()

    if not args.pg_dsn:
        raise SystemExit("pg dsn is required via --pg-dsn or WECHATCLI_PG_DSN")

    sqlite_conn = sqlite3.connect(args.sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    pg_storage = PostgresStorage(args.pg_dsn)
    pg_conn = pg_storage.conn

    try:
        if args.truncate:
            with pg_conn.cursor() as cur:
                cur.execute(
                    "TRUNCATE TABLE articles, login_sessions, accounts, meta RESTART IDENTITY CASCADE"
                )
            pg_conn.commit()

        with sqlite_conn:
            meta_rows = sqlite_conn.execute("SELECT key, value FROM meta").fetchall()
            if meta_rows:
                export_meta(pg_conn, [(row["key"], row["value"]) for row in meta_rows])

            account_rows = sqlite_conn.execute("SELECT * FROM accounts").fetchall()
            if account_rows:
                export_accounts(
                    pg_conn,
                    [
                        (
                            row["biz"],
                            row["nickname"],
                            row["alias"],
                            row["round_head_img"],
                            row["uin"],
                            row["key"],
                            row["pass_ticket"],
                            bool(row["is_default"]),
                            row["last_synced_at"],
                            row["created_at"],
                            row["updated_at"],
                        )
                        for row in account_rows
                    ],
                )

            login_rows = sqlite_conn.execute("SELECT * FROM login_sessions").fetchall()
            if login_rows:
                export_login_sessions(
                    pg_conn,
                    [
                        (
                            row["id"],
                            row["token"],
                            row["cookies_json"],
                            row["nickname"],
                            row["avatar"],
                            bool(row["is_default"]),
                            row["created_at"],
                            row["updated_at"],
                        )
                        for row in login_rows
                    ],
                )

            article_cursor = sqlite_conn.execute("SELECT * FROM articles")
            while True:
                batch = article_cursor.fetchmany(2000)
                if not batch:
                    break
                export_articles(
                    pg_conn,
                    [
                        (
                            row["biz"],
                            row["article_id"],
                            row["title"],
                            row["author"],
                            row["digest"],
                            row["cover"],
                            row["link"],
                            row["source_url"],
                            row["publish_at"],
                            row["raw_json"],
                            row["created_at"],
                            row["updated_at"],
                        )
                        for row in batch
                    ],
                )

        pg_conn.commit()
    finally:
        sqlite_conn.close()
        pg_storage.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
