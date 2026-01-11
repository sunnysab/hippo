#!/usr/bin/env python3
"""Export SQLite data to PostgreSQL."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import psycopg2
import psycopg2.extras
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from wechatcli.config import DB_PATH
from wechatcli.storage import ISO_FORMAT
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


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.strptime(value, ISO_FORMAT)
    return dt.replace(tzinfo=timezone.utc)


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

        total_meta = sqlite_conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
        total_accounts = sqlite_conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        total_logins = sqlite_conn.execute("SELECT COUNT(*) FROM login_sessions").fetchone()[0]
        total_articles = sqlite_conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]

        progress_columns = [
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TextColumn("{task.fields[rate]}"),
            TimeElapsedColumn(),
        ]
        with Progress(*progress_columns) as progress, sqlite_conn:
            meta_task = progress.add_task("导出 meta", total=total_meta, rate="")
            account_task = progress.add_task("导出 accounts", total=total_accounts, rate="")
            login_task = progress.add_task("导出 login_sessions", total=total_logins, rate="")
            article_task = progress.add_task("导出 articles", total=total_articles, rate="")

            if total_meta:
                meta_rows = sqlite_conn.execute("SELECT key, value FROM meta").fetchall()
                export_meta(pg_conn, [(row["key"], row["value"]) for row in meta_rows])
                progress.update(meta_task, completed=total_meta, rate=f"{total_meta}/s")

            if total_accounts:
                account_rows = sqlite_conn.execute("SELECT * FROM accounts").fetchall()
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
                            parse_ts(row["last_synced_at"]),
                            parse_ts(row["created_at"]),
                            parse_ts(row["updated_at"]),
                        )
                        for row in account_rows
                    ],
                )
                progress.update(account_task, completed=total_accounts, rate=f"{total_accounts}/s")

            if total_logins:
                login_rows = sqlite_conn.execute("SELECT * FROM login_sessions").fetchall()
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
                            parse_ts(row["created_at"]),
                            parse_ts(row["updated_at"]),
                        )
                        for row in login_rows
                    ],
                )
                progress.update(login_task, completed=total_logins, rate=f"{total_logins}/s")

            if total_articles:
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
                                parse_ts(row["created_at"]),
                                parse_ts(row["updated_at"]),
                            )
                            for row in batch
                        ],
                    )
                    completed = progress.tasks[article_task].completed + len(batch)
                    elapsed = progress.tasks[article_task].elapsed or 0.0
                    rate = f"{int(completed / elapsed)}/s" if elapsed > 0 else "-"
                    progress.update(article_task, completed=completed, rate=rate)

        pg_conn.commit()
    finally:
        sqlite_conn.close()
        pg_storage.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
