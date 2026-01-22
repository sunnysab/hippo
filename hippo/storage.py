"""SQLite-backed persistence for the CLI."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Protocol

import psycopg2
import psycopg2.extras

from .config import DB_PATH
from .models import AccountCredential, AccountGroup, ArticleRecord, LoginSession

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
SCHEMA_VERSION = "5"


class StorageInitError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.utcnow().strftime(ISO_FORMAT)


def _utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _session_identity(cookies: dict[str, str]) -> Optional[str]:
    for key in ("wxuin", "uin", "fakeuin", "mpuin"):
        value = cookies.get(key)
        if value:
            return f"{key}:{value}"
    return None


class StorageLike(Protocol):
    def close(self) -> None: ...
    def get_meta(self, key: str) -> Optional[str]: ...
    def set_meta(self, key: str, value: str) -> None: ...
    def delete_meta(self, key: str) -> None: ...
    def upsert_account(self, account: AccountCredential) -> AccountCredential: ...
    def list_accounts(self, group: Optional[str] = None) -> List[AccountCredential]: ...
    def get_account(
        self, biz: Optional[str] = None, *, fallback_to_default: bool = True
    ) -> AccountCredential: ...
    def remove_account(self, biz: str) -> int: ...
    def set_default_account(self, biz: str) -> None: ...
    def upsert_group(self, name: str) -> AccountGroup: ...
    def list_groups(self) -> List[AccountGroup]: ...
    def set_account_group(self, biz: str, group_name: Optional[str]) -> None: ...
    def update_last_synced(self, biz: str) -> None: ...
    def set_account_disabled(self, biz: str, is_disabled: bool) -> None: ...
    def save_login_session(
        self, session: LoginSession, *, set_default: bool = True
    ) -> LoginSession: ...
    def get_login_session(self) -> LoginSession: ...
    def save_articles(self, articles: Iterable[ArticleRecord]) -> int: ...
    def list_articles(
        self, biz: str, *, limit: int = 10, since_timestamp: Optional[int] = None
    ) -> List[ArticleRecord]: ...
    def get_existing_article_ids(self, biz: str, article_ids: Iterable[str]) -> set[str]: ...


class Storage(AbstractContextManager):
    """Simple wrapper around sqlite3 with a fixed schema."""

    def __init__(self, db_path: Path | str = DB_PATH, *, auto_init: bool = False) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        if auto_init:
            self._init_db()
        else:
            try:
                self._ensure_initialized()
            except Exception:
                self.conn.close()
                raise

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        self.close()

    def close(self) -> None:
        self.conn.close()

    def _init_db(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON;")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS account_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                biz TEXT PRIMARY KEY,
                nickname TEXT NOT NULL,
                alias TEXT,
                round_head_img TEXT,
                uin TEXT NOT NULL,
                key TEXT NOT NULL,
                pass_ticket TEXT NOT NULL,
                group_id INTEGER REFERENCES account_groups(id) ON DELETE SET NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                is_disabled INTEGER NOT NULL DEFAULT 0,
                last_synced_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_accounts_group
            ON accounts (group_id)
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                biz TEXT NOT NULL REFERENCES accounts(biz) ON DELETE CASCADE,
                article_id TEXT NOT NULL,
                title TEXT NOT NULL,
                author TEXT,
                digest TEXT,
                cover TEXT,
                link TEXT NOT NULL,
                source_url TEXT,
                publish_at INTEGER,
                raw_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (biz, article_id)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_articles_biz_publish
            ON articles (biz, publish_at DESC)
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS login_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL,
                cookies_json TEXT NOT NULL,
                nickname TEXT,
                avatar TEXT,
                is_default INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        row = cur.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        current_version = row["value"] if row else None
        self._migrate_schema(cur, current_version)
        cur.execute(
            """
            INSERT OR REPLACE INTO meta(key, value)
            VALUES ('schema_version', ?)
            """,
            (SCHEMA_VERSION,)
        )
        self.conn.commit()

    def _ensure_initialized(self) -> None:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
        ).fetchone()
        if not row:
            raise StorageInitError("Database not initialized. Run `python -m hippo db init`.")
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if not row:
            raise StorageInitError("Database not initialized. Run `python -m hippo db init`.")
        current_version = row["value"]
        if current_version != SCHEMA_VERSION:
            raise StorageInitError(
                "Database schema out of date. Run `python -m hippo db init` to migrate."
            )

    def _migrate_schema(self, cur: sqlite3.Cursor, current_version: Optional[str]) -> None:
        if current_version == SCHEMA_VERSION:
            return
        if current_version in (None, "2"):
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS articles_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    biz TEXT NOT NULL REFERENCES accounts(biz) ON DELETE CASCADE,
                    article_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    author TEXT,
                    digest TEXT,
                    cover TEXT,
                    link TEXT NOT NULL,
                    source_url TEXT,
                    publish_at INTEGER,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (biz, article_id)
                )
                """
            )
            cur.execute(
                """
                INSERT OR REPLACE INTO articles_new
                    (id, biz, article_id, title, author, digest, cover, link,
                     source_url, publish_at, raw_json, created_at, updated_at)
                SELECT id, biz, article_id, title, author, digest, cover, link,
                       source_url, publish_at, raw_json, created_at, updated_at
                FROM articles
                ORDER BY updated_at
                """
            )
            cur.execute("DROP TABLE articles")
            cur.execute("ALTER TABLE articles_new RENAME TO articles")
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_articles_biz_publish
                ON articles (biz, publish_at DESC)
                """
            )
        if current_version in (None, "2", "3"):
            self._ensure_account_disabled_column(cur)
        if current_version in (None, "2", "3", "4"):
            self._ensure_account_groups_table(cur)
            self._ensure_group_id_column(cur)
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_accounts_group
                ON accounts (group_id)
                """
            )

    @staticmethod
    def _ensure_account_disabled_column(cur: sqlite3.Cursor) -> None:
        columns = {
            row[1] for row in cur.execute("PRAGMA table_info(accounts)").fetchall()
        }
        if "is_disabled" not in columns:
            cur.execute(
                "ALTER TABLE accounts ADD COLUMN is_disabled INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    def _ensure_account_groups_table(cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS account_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _ensure_group_id_column(cur: sqlite3.Cursor) -> None:
        columns = {
            row[1] for row in cur.execute("PRAGMA table_info(accounts)").fetchall()
        }
        if "group_id" not in columns:
            cur.execute("ALTER TABLE accounts ADD COLUMN group_id INTEGER")

    # Meta helpers --------------------------------------------------------
    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value)
        )
        self.conn.commit()

    def delete_meta(self, key: str) -> None:
        self.conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        self.conn.commit()

    # Account helpers -----------------------------------------------------
    def upsert_account(self, account: AccountCredential) -> AccountCredential:
        now = _utc_now()
        self.conn.execute(
            """
            INSERT INTO accounts (biz, nickname, alias, round_head_img, uin, key, pass_ticket,
                                  group_id, is_default, is_disabled, last_synced_at, created_at, updated_at)
            VALUES (:biz, :nickname, :alias, :round_head_img, :uin, :key, :pass_ticket,
                    :group_id, :is_default, :is_disabled, :last_synced_at, :created_at, :updated_at)
            ON CONFLICT(biz) DO UPDATE SET
                nickname=excluded.nickname,
                alias=excluded.alias,
                round_head_img=excluded.round_head_img,
                uin=excluded.uin,
                key=excluded.key,
                pass_ticket=excluded.pass_ticket,
                updated_at=excluded.updated_at
            """,
            {
                "biz": account.biz,
                "nickname": account.nickname,
                "alias": account.alias,
                "round_head_img": account.round_head_img,
                "uin": account.uin or "",
                "key": account.key or "",
                "pass_ticket": account.pass_ticket or "",
                "group_id": account.group_id,
                "is_default": 1 if account.is_default else 0,
                "is_disabled": 1 if account.is_disabled else 0,
                "last_synced_at": account.last_synced_at.strftime(ISO_FORMAT)
                if account.last_synced_at
                else None,
                "created_at": now,
                "updated_at": now,
            },
        )
        self.conn.commit()
        if account.is_default:
            self.set_default_account(account.biz)
        return self.get_account(account.biz, fallback_to_default=False)

    def list_accounts(self, group: Optional[str] = None) -> List[AccountCredential]:
        query = """
            SELECT a.*, g.name AS group_name
            FROM accounts a
            LEFT JOIN account_groups g ON g.id = a.group_id
        """
        params: list = []
        if group:
            query += " WHERE g.name = ?"
            params.append(group)
        query += " ORDER BY a.is_default DESC, a.nickname ASC"
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_account(row) for row in rows]

    def get_account(
        self, biz: Optional[str] = None, *, fallback_to_default: bool = True
    ) -> AccountCredential:
        row = None
        if biz:
            row = self.conn.execute(
                """
                SELECT a.*, g.name AS group_name
                FROM accounts a
                LEFT JOIN account_groups g ON g.id = a.group_id
                WHERE a.biz = ?
                """,
                (biz,),
            ).fetchone()
        if not row and fallback_to_default:
            row = self.conn.execute(
                """
                SELECT a.*, g.name AS group_name
                FROM accounts a
                LEFT JOIN account_groups g ON g.id = a.group_id
                WHERE a.is_default = 1
                LIMIT 1
                """
            ).fetchone()
        if not row and fallback_to_default and not biz:
            row = self.conn.execute(
                """
                SELECT a.*, g.name AS group_name
                FROM accounts a
                LEFT JOIN account_groups g ON g.id = a.group_id
                ORDER BY a.updated_at DESC
                LIMIT 1
                """
            ).fetchone()
        if not row:
            raise LookupError(
                "No account found. Create one with `accounts add` or `accounts search --interactive`."
            )
        return self._row_to_account(row)

    def remove_account(self, biz: str) -> int:
        cur = self.conn.execute("DELETE FROM accounts WHERE biz = ?", (biz,))
        self.conn.commit()
        return cur.rowcount

    def set_default_account(self, biz: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE accounts SET is_default = 0")
            updated = self.conn.execute(
                "UPDATE accounts SET is_default = 1 WHERE biz = ?", (biz,)
            ).rowcount
            if updated == 0:
                raise LookupError(f"Account {biz} not found")

    def upsert_group(self, name: str) -> AccountGroup:
        trimmed = name.strip()
        if not trimmed:
            raise ValueError("Group name cannot be empty.")
        now = _utc_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO account_groups (name, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (trimmed, now, now),
            )
            row = self.conn.execute(
                "SELECT id, name FROM account_groups WHERE name = ?", (trimmed,)
            ).fetchone()
        if not row:
            raise RuntimeError(f"Failed to create group {trimmed}.")
        return AccountGroup(id=row["id"], name=row["name"])

    def list_groups(self) -> List[AccountGroup]:
        rows = self.conn.execute(
            """
            SELECT g.id, g.name, COUNT(a.biz) AS account_count
            FROM account_groups g
            LEFT JOIN accounts a ON a.group_id = g.id
            GROUP BY g.id, g.name
            ORDER BY g.name ASC
            """
        ).fetchall()
        return [
            AccountGroup(id=row["id"], name=row["name"], account_count=row["account_count"])
            for row in rows
        ]

    def set_account_group(self, biz: str, group_name: Optional[str]) -> None:
        target_name = group_name.strip() if group_name else ""
        with self.conn:
            if not target_name:
                updated = self.conn.execute(
                    "UPDATE accounts SET group_id = NULL, updated_at = ? WHERE biz = ?",
                    (_utc_now(), biz),
                ).rowcount
                if updated == 0:
                    raise LookupError(f"Account {biz} not found")
                return
            group = self.upsert_group(target_name)
            updated = self.conn.execute(
                "UPDATE accounts SET group_id = ?, updated_at = ? WHERE biz = ?",
                (group.id, _utc_now(), biz),
            ).rowcount
            if updated == 0:
                raise LookupError(f"Account {biz} not found")

    def update_last_synced(self, biz: str) -> None:
        self.conn.execute(
            "UPDATE accounts SET last_synced_at = ?, updated_at = ? WHERE biz = ?",
            (_utc_now(), _utc_now(), biz),
        )
        self.conn.commit()

    def set_account_disabled(self, biz: str, is_disabled: bool) -> None:
        with self.conn:
            updated = self.conn.execute(
                "UPDATE accounts SET is_disabled = ?, updated_at = ? WHERE biz = ?",
                (1 if is_disabled else 0, _utc_now(), biz),
            ).rowcount
            if updated == 0:
                raise LookupError(f"Account {biz} not found")

    # Login session helpers ------------------------------------------------
    def save_login_session(self, session: LoginSession, *, set_default: bool = True) -> LoginSession:
        now = _utc_now()
        cookie_json = json.dumps(session.cookies, ensure_ascii=False)
        session_identity = _session_identity(session.cookies)
        rows = self.conn.execute(
            "SELECT id, cookies_json, nickname FROM login_sessions ORDER BY id DESC"
        ).fetchall()
        match_id: Optional[int] = None
        if session_identity:
            for row in rows:
                try:
                    row_cookies = json.loads(row["cookies_json"])
                except Exception:
                    continue
                if _session_identity(row_cookies) == session_identity:
                    match_id = row["id"]
                    break
        if match_id is None and session.nickname:
            target_name = session.nickname.strip().lower()
            for row in rows:
                nickname = (row["nickname"] or "").strip().lower()
                if nickname and nickname == target_name:
                    match_id = row["id"]
                    break
        if match_id is not None:
            with self.conn:
                if set_default:
                    self.conn.execute("UPDATE login_sessions SET is_default = 0")
                self.conn.execute(
                    """
                    UPDATE login_sessions
                    SET token = ?, cookies_json = ?, nickname = ?, avatar = ?, is_default = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        session.token,
                        cookie_json,
                        session.nickname,
                        session.avatar,
                        1 if set_default else 0,
                        now,
                        match_id,
                    ),
                )
            return self.get_login_session()
        if set_default:
            self.conn.execute("UPDATE login_sessions SET is_default = 0")
        self.conn.execute(
            """
            INSERT INTO login_sessions (token, cookies_json, nickname, avatar, is_default, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.token,
                cookie_json,
                session.nickname,
                session.avatar,
                1 if set_default else 0,
                now,
                now,
            ),
        )
        self.conn.commit()
        return self.get_login_session()

    def get_login_session(self) -> LoginSession:
        row = self.conn.execute(
            "SELECT * FROM login_sessions WHERE is_default = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise LookupError("No login session found. Run `hippo login` first.")
        cookies = json.loads(row["cookies_json"])
        return LoginSession(
            token=row["token"],
            cookies=cookies,
            nickname=row["nickname"],
            avatar=row["avatar"],
        )

    # Article helpers -----------------------------------------------------
    def save_articles(self, articles: Iterable[ArticleRecord]) -> int:
        now = _utc_now()
        inserted = 0
        with self.conn:
            for article in articles:
                payload = {
                    "biz": article.biz,
                    "article_id": article.article_id,
                    "title": article.title,
                    "author": article.author,
                    "digest": article.digest,
                    "cover": article.cover,
                    "link": article.link,
                    "source_url": article.source_url,
                    "publish_at": article.publish_at,
                    "raw_json": json.dumps(article.raw, ensure_ascii=False),
                    "created_at": now,
                    "updated_at": now,
                }
                cur = self.conn.execute(
                    """
                    INSERT INTO articles
                        (biz, article_id, title, author, digest, cover, link, source_url,
                         publish_at, raw_json, created_at, updated_at)
                    VALUES (:biz, :article_id, :title, :author, :digest, :cover, :link,
                            :source_url, :publish_at, :raw_json, :created_at, :updated_at)
                    ON CONFLICT(biz, article_id) DO UPDATE SET
                        title=excluded.title,
                        author=excluded.author,
                        digest=excluded.digest,
                        cover=excluded.cover,
                        link=excluded.link,
                        source_url=excluded.source_url,
                        publish_at=excluded.publish_at,
                        raw_json=excluded.raw_json,
                        updated_at=excluded.updated_at
                    """,
                    payload,
                )
                inserted += cur.rowcount
        return inserted

    def list_articles(
        self,
        biz: str,
        *,
        limit: Optional[int] = 10,
        since_timestamp: Optional[int] = None,
    ) -> List[ArticleRecord]:
        query = "SELECT * FROM articles WHERE biz = ?"
        params: list = [biz]
        if since_timestamp is not None:
            query += " AND (publish_at IS NULL OR publish_at >= ?)"
            params.append(since_timestamp)
        query += " ORDER BY publish_at IS NULL, publish_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_article(row) for row in rows]

    def get_existing_article_ids(self, biz: str, article_ids: Iterable[str]) -> set[str]:
        ids = [item for item in article_ids if item]
        if not ids:
            return set()
        existing: set[str] = set()
        chunk_size = 900
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i : i + chunk_size]
            placeholders = ",".join(["?"] * len(chunk))
            query = (
                f"SELECT article_id FROM articles WHERE biz = ? AND article_id IN ({placeholders})"
            )
            rows = self.conn.execute(query, [biz, *chunk]).fetchall()
            existing.update(row["article_id"] for row in rows)
        return existing

    # Internal helpers ----------------------------------------------------
    @staticmethod
    def _row_to_account(row: sqlite3.Row) -> AccountCredential:
        last_synced_at = None
        if row["last_synced_at"]:
            last_synced_at = datetime.strptime(row["last_synced_at"], ISO_FORMAT)
        return AccountCredential(
            biz=row["biz"],
            nickname=row["nickname"],
            alias=row["alias"],
            round_head_img=row["round_head_img"],
            uin=row["uin"] or "",
            key=row["key"] or "",
            pass_ticket=row["pass_ticket"] or "",
            is_default=bool(row["is_default"]),
            is_disabled=bool(row["is_disabled"]) if "is_disabled" in row.keys() else False,
            last_synced_at=last_synced_at,
            group_id=row["group_id"] if "group_id" in row.keys() else None,
            group_name=row["group_name"] if "group_name" in row.keys() else None,
        )

    @staticmethod
    def _row_to_article(row: sqlite3.Row) -> ArticleRecord:
        raw = json.loads(row["raw_json"])
        return ArticleRecord(
            biz=row["biz"],
            article_id=row["article_id"],
            title=row["title"],
            author=row["author"],
            digest=row["digest"],
            cover=row["cover"],
            link=row["link"],
            source_url=row["source_url"],
            publish_at=row["publish_at"],
            raw=raw,
        )


class PostgresStorage(AbstractContextManager):
    def __init__(self, dsn: str, *, auto_init: bool = False) -> None:
        self.dsn = dsn
        self.conn = psycopg2.connect(dsn, options="-c timezone=Asia/Shanghai")
        self.conn.autocommit = False
        if auto_init:
            self._init_db()
        else:
            try:
                self._ensure_initialized()
            except Exception:
                self.conn.close()
                raise

    def __enter__(self) -> "PostgresStorage":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        self.close()

    def close(self) -> None:
        self.conn.close()

    def _init_db(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS account_groups (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    biz TEXT PRIMARY KEY,
                    nickname TEXT NOT NULL,
                    alias TEXT,
                    round_head_img TEXT,
                    uin TEXT NOT NULL,
                    key TEXT NOT NULL,
                    pass_ticket TEXT NOT NULL,
                    group_id INTEGER REFERENCES account_groups(id) ON DELETE SET NULL,
                    is_default BOOLEAN NOT NULL DEFAULT FALSE,
                    is_disabled BOOLEAN NOT NULL DEFAULT FALSE,
                    last_synced_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            cur.execute(
                """
                ALTER TABLE accounts
                ADD COLUMN IF NOT EXISTS is_disabled BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE accounts
                ADD COLUMN IF NOT EXISTS group_id INTEGER REFERENCES account_groups(id) ON DELETE SET NULL
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_accounts_group
                ON accounts (group_id)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    id SERIAL PRIMARY KEY,
                    biz TEXT NOT NULL REFERENCES accounts(biz) ON DELETE CASCADE,
                    article_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    author TEXT,
                    digest TEXT,
                    cover TEXT,
                    link TEXT NOT NULL,
                    source_url TEXT,
                    publish_at BIGINT,
                    raw_json TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL,
                    UNIQUE (biz, article_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS article_content (
                    id SERIAL PRIMARY KEY,
                    article_pk INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                    url_token TEXT,
                    clean_html TEXT,
                    content_markdown TEXT,
                    content_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL,
                    UNIQUE (article_pk)
                )
                """
            )
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'articles'
                AND column_name IN (
                    'url_token',
                    'clean_html',
                    'content_markdown',
                    'content_json'
                )
                """
            )
            existing_columns = {row[0] for row in cur.fetchall()}
            if {
                "url_token",
                "clean_html",
                "content_markdown",
                "content_json",
            }.issubset(existing_columns):
                cur.execute(
                    """
                    INSERT INTO article_content
                        (article_pk, url_token, clean_html, content_markdown, content_json,
                         created_at, updated_at)
                    SELECT
                        id,
                        url_token,
                        clean_html,
                        content_markdown,
                        content_json::jsonb,
                        created_at,
                        updated_at
                    FROM articles
                    WHERE url_token IS NOT NULL
                       OR clean_html IS NOT NULL
                       OR content_markdown IS NOT NULL
                       OR content_json IS NOT NULL
                    ON CONFLICT (article_pk) DO UPDATE SET
                        url_token=EXCLUDED.url_token,
                        clean_html=EXCLUDED.clean_html,
                        content_markdown=EXCLUDED.content_markdown,
                        content_json=EXCLUDED.content_json,
                        updated_at=EXCLUDED.updated_at
                    """
                )
            cur.execute(
                """
                ALTER TABLE articles
                DROP COLUMN IF EXISTS url_token,
                DROP COLUMN IF EXISTS clean_html,
                DROP COLUMN IF EXISTS content_markdown,
                DROP COLUMN IF EXISTS content_json,
                DROP COLUMN IF EXISTS cover_image_id
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_articles_biz_publish
                ON articles (biz, publish_at DESC)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS article_images (
                    id SERIAL PRIMARY KEY,
                    article_pk INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    orig_url TEXT,
                    content_type TEXT,
                    data BYTEA,
                    failed_at TIMESTAMPTZ,
                    failed_reason TEXT,
                    updated_at TIMESTAMPTZ NOT NULL,
                    UNIQUE (article_pk, orig_url)
                )
                """
            )
            cur.execute(
                """
                ALTER TABLE article_images
                ADD COLUMN IF NOT EXISTS failed_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS failed_reason TEXT
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS login_sessions (
                    id SERIAL PRIMARY KEY,
                    token TEXT NOT NULL,
                    cookies_json TEXT NOT NULL,
                    nickname TEXT,
                    avatar TEXT,
                    is_default BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            cur.execute(
                """
                INSERT INTO meta(key, value)
                VALUES ('schema_version', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                (SCHEMA_VERSION,),
            )
        self.conn.commit()

    def _ensure_initialized(self) -> None:
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.meta')")
                table_name = cur.fetchone()[0]
                if not table_name:
                    raise StorageInitError(
                        "Database not initialized. Run `python -m hippo db init`."
                    )
                cur.execute("SELECT value FROM meta WHERE key = %s", ("schema_version",))
                row = cur.fetchone()
                if not row:
                    raise StorageInitError(
                        "Database not initialized. Run `python -m hippo db init`."
                    )
                current_version = row[0]
                if current_version != SCHEMA_VERSION:
                    raise StorageInitError(
                        "Database schema out of date. Run `python -m hippo db init` to migrate."
                    )
            self.conn.rollback()
        except Exception:
            self.conn.rollback()
            raise
    # Meta helpers --------------------------------------------------------
    def get_meta(self, key: str) -> Optional[str]:
        with self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT value FROM meta WHERE key = %s", (key,))
            row = cur.fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO meta(key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, value),
            )
        self.conn.commit()

    def delete_meta(self, key: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM meta WHERE key = %s", (key,))
        self.conn.commit()

    # Account helpers -----------------------------------------------------
    def upsert_account(self, account: AccountCredential) -> AccountCredential:
        now = _utc_now_dt()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO accounts (biz, nickname, alias, round_head_img, uin, key, pass_ticket,
                                      group_id, is_default, is_disabled, last_synced_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (biz) DO UPDATE SET
                    nickname=EXCLUDED.nickname,
                    alias=EXCLUDED.alias,
                    round_head_img=EXCLUDED.round_head_img,
                    uin=EXCLUDED.uin,
                    key=EXCLUDED.key,
                    pass_ticket=EXCLUDED.pass_ticket,
                    updated_at=EXCLUDED.updated_at
                """,
                (
                    account.biz,
                    account.nickname,
                    account.alias,
                    account.round_head_img,
                    account.uin or "",
                    account.key or "",
                    account.pass_ticket or "",
                    account.group_id,
                    account.is_default,
                    account.is_disabled,
                    account.last_synced_at,
                    now,
                    now,
                ),
            )
        self.conn.commit()
        if account.is_default:
            self.set_default_account(account.biz)
        return self.get_account(account.biz, fallback_to_default=False)

    def list_accounts(self, group: Optional[str] = None) -> List[AccountCredential]:
        query = """
            SELECT a.*, g.name AS group_name
            FROM accounts a
            LEFT JOIN account_groups g ON g.id = a.group_id
        """
        params: list = []
        if group:
            query += " WHERE g.name = %s"
            params.append(group)
        query += " ORDER BY a.is_default DESC, a.nickname ASC"
        with self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return [self._row_to_account(row) for row in rows]

    def get_account(
        self, biz: Optional[str] = None, *, fallback_to_default: bool = True
    ) -> AccountCredential:
        row = None
        with self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if biz:
                cur.execute(
                    """
                    SELECT a.*, g.name AS group_name
                    FROM accounts a
                    LEFT JOIN account_groups g ON g.id = a.group_id
                    WHERE a.biz = %s
                    """,
                    (biz,),
                )
                row = cur.fetchone()
            if not row and fallback_to_default:
                cur.execute(
                    """
                    SELECT a.*, g.name AS group_name
                    FROM accounts a
                    LEFT JOIN account_groups g ON g.id = a.group_id
                    WHERE a.is_default = TRUE
                    LIMIT 1
                    """
                )
                row = cur.fetchone()
            if not row and fallback_to_default and not biz:
                cur.execute(
                    """
                    SELECT a.*, g.name AS group_name
                    FROM accounts a
                    LEFT JOIN account_groups g ON g.id = a.group_id
                    ORDER BY a.updated_at DESC
                    LIMIT 1
                    """
                )
                row = cur.fetchone()
        if not row:
            raise LookupError("No account found. Create one with `accounts add` or `accounts search --interactive`.")
        return self._row_to_account(row)

    def remove_account(self, biz: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM accounts WHERE biz = %s", (biz,))
            removed = cur.rowcount
        self.conn.commit()
        return removed

    def set_default_account(self, biz: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute("UPDATE accounts SET is_default = FALSE")
            cur.execute("UPDATE accounts SET is_default = TRUE WHERE biz = %s", (biz,))
            updated = cur.rowcount
        self.conn.commit()
        if updated == 0:
            raise LookupError(f"Account {biz} not found")

    def upsert_group(self, name: str) -> AccountGroup:
        trimmed = name.strip()
        if not trimmed:
            raise ValueError("Group name cannot be empty.")
        now = _utc_now_dt()
        with self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                INSERT INTO account_groups (name, created_at, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET updated_at = EXCLUDED.updated_at
                RETURNING id, name
                """,
                (trimmed, now, now),
            )
            row = cur.fetchone()
        self.conn.commit()
        if not row:
            raise RuntimeError(f"Failed to create group {trimmed}.")
        return AccountGroup(id=row["id"], name=row["name"])

    def list_groups(self) -> List[AccountGroup]:
        with self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                SELECT g.id, g.name, COUNT(a.biz) AS account_count
                FROM account_groups g
                LEFT JOIN accounts a ON a.group_id = g.id
                GROUP BY g.id, g.name
                ORDER BY g.name ASC
                """
            )
            rows = cur.fetchall()
        return [
            AccountGroup(id=row["id"], name=row["name"], account_count=row["account_count"])
            for row in rows
        ]

    def set_account_group(self, biz: str, group_name: Optional[str]) -> None:
        target_name = group_name.strip() if group_name else ""
        now = _utc_now_dt()
        with self.conn.cursor() as cur:
            if not target_name:
                cur.execute(
                    "UPDATE accounts SET group_id = NULL, updated_at = %s WHERE biz = %s",
                    (now, biz),
                )
                updated = cur.rowcount
                self.conn.commit()
                if updated == 0:
                    raise LookupError(f"Account {biz} not found")
                return
        group = self.upsert_group(target_name)
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE accounts SET group_id = %s, updated_at = %s WHERE biz = %s",
                (group.id, now, biz),
            )
            updated = cur.rowcount
        self.conn.commit()
        if updated == 0:
            raise LookupError(f"Account {biz} not found")

    def update_last_synced(self, biz: str) -> None:
        now = _utc_now_dt()
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE accounts SET last_synced_at = %s, updated_at = %s WHERE biz = %s",
                (now, now, biz),
            )
        self.conn.commit()

    def set_account_disabled(self, biz: str, is_disabled: bool) -> None:
        now = _utc_now_dt()
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE accounts SET is_disabled = %s, updated_at = %s WHERE biz = %s",
                (is_disabled, now, biz),
            )
            updated = cur.rowcount
        self.conn.commit()
        if updated == 0:
            raise LookupError(f"Account {biz} not found")

    # Login session helpers ------------------------------------------------
    def save_login_session(self, session: LoginSession, *, set_default: bool = True) -> LoginSession:
        now = _utc_now_dt()
        cookie_json = json.dumps(session.cookies, ensure_ascii=False)
        session_identity = _session_identity(session.cookies)
        with self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id, cookies_json, nickname FROM login_sessions ORDER BY id DESC")
            rows = cur.fetchall()
        match_id: Optional[int] = None
        if session_identity:
            for row in rows:
                try:
                    row_cookies = json.loads(row["cookies_json"])
                except Exception:
                    continue
                if _session_identity(row_cookies) == session_identity:
                    match_id = row["id"]
                    break
        if match_id is None and session.nickname:
            target_name = session.nickname.strip().lower()
            for row in rows:
                nickname = (row["nickname"] or "").strip().lower()
                if nickname and nickname == target_name:
                    match_id = row["id"]
                    break
        if match_id is not None:
            with self.conn.cursor() as cur:
                if set_default:
                    cur.execute("UPDATE login_sessions SET is_default = FALSE")
                cur.execute(
                    """
                    UPDATE login_sessions
                    SET token = %s, cookies_json = %s, nickname = %s, avatar = %s,
                        is_default = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        session.token,
                        cookie_json,
                        session.nickname,
                        session.avatar,
                        True if set_default else False,
                        now,
                        match_id,
                    ),
                )
            self.conn.commit()
            return self.get_login_session()
        for attempt in range(2):
            try:
                with self.conn.cursor() as cur:
                    if set_default:
                        cur.execute("UPDATE login_sessions SET is_default = FALSE")
                    cur.execute(
                        """
                        INSERT INTO login_sessions (token, cookies_json, nickname, avatar, is_default, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            session.token,
                            cookie_json,
                            session.nickname,
                            session.avatar,
                            True if set_default else False,
                            now,
                            now,
                        ),
                    )
                self.conn.commit()
                return self.get_login_session()
            except psycopg2.errors.UniqueViolation:
                self.conn.rollback()
                if attempt == 0:
                    with self.conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT setval(
                                pg_get_serial_sequence('login_sessions', 'id'),
                                COALESCE((SELECT MAX(id) FROM login_sessions), 1),
                                (SELECT COUNT(*) FROM login_sessions) > 0
                            )
                            """
                        )
                    self.conn.commit()
                    continue
                raise
            except Exception:
                self.conn.rollback()
                raise

    def get_login_session(self) -> LoginSession:
        with self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM login_sessions WHERE is_default = TRUE ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        if not row:
            raise LookupError("No login session found. Run `hippo login` first.")
        cookies = json.loads(row["cookies_json"])
        return LoginSession(
            token=row["token"],
            cookies=cookies,
            nickname=row["nickname"],
            avatar=row["avatar"],
        )

    # Article helpers -----------------------------------------------------
    def save_articles(self, articles: Iterable[ArticleRecord]) -> int:
        now = _utc_now_dt()
        inserted = 0
        with self.conn.cursor() as cur:
            for article in articles:
                cur.execute(
                    """
                    INSERT INTO articles
                        (biz, article_id, title, author, digest, cover, link, source_url,
                         publish_at, raw_json, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s)
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
                    (
                        article.biz,
                        article.article_id,
                        article.title,
                        article.author,
                        article.digest,
                        article.cover,
                        article.link,
                        article.source_url,
                        article.publish_at,
                        json.dumps(article.raw, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                inserted += cur.rowcount
        self.conn.commit()
        return inserted

    def save_article_content(
        self,
        article: ArticleRecord,
        *,
        url_token: Optional[str],
        title: str,
        clean_html: str,
        content_markdown: str,
        content_blocks: list[dict],
        cover_url: Optional[str],
        images: list[dict],
    ) -> None:
        now = _utc_now_dt()
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO articles (
                        biz, article_id, title, author, digest, cover, link, source_url,
                        publish_at, raw_json, created_at, updated_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s
                    )
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
                    RETURNING id
                    """,
                    (
                        article.biz,
                        article.article_id,
                        title,
                        article.author,
                        article.digest,
                        cover_url,
                        article.link,
                        article.source_url,
                        article.publish_at,
                        json.dumps(article.raw, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                article_pk = cur.fetchone()[0]

                cur.execute("DELETE FROM article_images WHERE article_pk = %s", (article_pk,))
                image_id_map: dict[str, int] = {}
                seen_orig_urls: set[str] = set()
                for image in images:
                    orig_url = image.get("orig_url")
                    if orig_url:
                        orig_url = str(orig_url)
                        if orig_url in seen_orig_urls:
                            continue
                        seen_orig_urls.add(orig_url)
                    cur.execute(
                        """
                        INSERT INTO article_images
                            (article_pk, position, kind, orig_url, content_type, data, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            article_pk,
                            image.get("position", 0),
                            image.get("kind", "inline"),
                            orig_url,
                            image.get("content_type"),
                            psycopg2.Binary(image.get("data")) if image.get("data") else None,
                            now,
                        ),
                    )
                    image_id = cur.fetchone()[0]
                    if orig_url:
                        image_id_map[orig_url] = image_id

                updated_blocks: list[dict] = []
                for block in content_blocks:
                    if block.get("type") == "image":
                        orig_url = block.get("orig_url")
                        image_id = image_id_map.get(str(orig_url)) if orig_url else None
                        updated = dict(block)
                        if image_id is not None:
                            updated["image_id"] = image_id
                        updated_blocks.append(updated)
                    else:
                        updated_blocks.append(block)

                cur.execute(
                    """
                    INSERT INTO article_content
                        (article_pk, url_token, clean_html, content_markdown, content_json, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (article_pk) DO UPDATE SET
                        url_token=EXCLUDED.url_token,
                        clean_html=EXCLUDED.clean_html,
                        content_markdown=EXCLUDED.content_markdown,
                        content_json=EXCLUDED.content_json,
                        updated_at=EXCLUDED.updated_at
                    """,
                    (
                        article_pk,
                        url_token,
                        clean_html,
                        content_markdown,
                        psycopg2.extras.Json(updated_blocks),
                        now,
                        now,
                    ),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def has_article_content(self, biz: str, article_id: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM article_content c
                JOIN articles a ON a.id = c.article_pk
                WHERE a.biz = %s AND a.article_id = %s
                LIMIT 1
                """,
                (biz, article_id),
            )
            return cur.fetchone() is not None

    def get_article_content_ids(self, biz: str, article_ids: Iterable[str]) -> set[str]:
        ids = [item for item in article_ids if item]
        if not ids:
            return set()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.article_id
                FROM article_content c
                JOIN articles a ON a.id = c.article_pk
                WHERE a.biz = %s AND a.article_id = ANY(%s)
                """,
                (biz, ids),
            )
            return {row[0] for row in cur.fetchall()}

    def update_article_image_data(
        self,
        biz: str,
        article_id: str,
        orig_url: str,
        content_type: Optional[str],
        data: bytes,
    ) -> None:
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM articles WHERE biz = %s AND article_id = %s",
                    (biz, article_id),
                )
                row = cur.fetchone()
                if not row:
                    return
                article_pk = row[0]
                cur.execute(
                    """
                    UPDATE article_images
                    SET content_type = %s,
                        data = %s,
                        failed_at = NULL,
                        failed_reason = NULL,
                        updated_at = %s
                    WHERE article_pk = %s AND orig_url = %s
                    """,
                    (
                        content_type,
                        psycopg2.Binary(data) if data else None,
                        _utc_now_dt(),
                        article_pk,
                        orig_url,
                    ),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def mark_article_image_failed(
        self,
        biz: str,
        article_id: str,
        orig_url: str,
        reason: str,
    ) -> None:
        trimmed = reason.strip()
        if len(trimmed) > 5000:
            trimmed = trimmed[:5000]
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM articles WHERE biz = %s AND article_id = %s",
                    (biz, article_id),
                )
                row = cur.fetchone()
                if not row:
                    return
                article_pk = row[0]
                cur.execute(
                    """
                    UPDATE article_images
                    SET failed_at = %s,
                        failed_reason = %s,
                        updated_at = %s
                    WHERE article_pk = %s AND orig_url = %s
                    """,
                    (
                        _utc_now_dt(),
                        trimmed,
                        _utc_now_dt(),
                        article_pk,
                        orig_url,
                    ),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def list_articles(
        self,
        biz: str,
        *,
        limit: Optional[int] = 10,
        since_timestamp: Optional[int] = None,
    ) -> List[ArticleRecord]:
        query = "SELECT * FROM articles WHERE biz = %s"
        params: list = [biz]
        if since_timestamp is not None:
            query += " AND (publish_at IS NULL OR publish_at >= %s)"
            params.append(since_timestamp)
        query += " ORDER BY publish_at IS NULL, publish_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        with self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return [self._row_to_article(row) for row in rows]

    def get_existing_article_ids(self, biz: str, article_ids: Iterable[str]) -> set[str]:
        ids = [item for item in article_ids if item]
        if not ids:
            return set()
        existing: set[str] = set()
        chunk_size = 900
        with self.conn.cursor() as cur:
            for i in range(0, len(ids), chunk_size):
                chunk = ids[i : i + chunk_size]
                cur.execute(
                    "SELECT article_id FROM articles WHERE biz = %s AND article_id = ANY(%s)",
                    (biz, chunk),
                )
                existing.update(row[0] for row in cur.fetchall())
        return existing

    # Internal helpers ----------------------------------------------------
    @staticmethod
    def _row_to_account(row: psycopg2.extras.DictRow) -> AccountCredential:
        last_synced_at = row["last_synced_at"] if row["last_synced_at"] else None
        return AccountCredential(
            biz=row["biz"],
            nickname=row["nickname"],
            alias=row["alias"],
            round_head_img=row["round_head_img"],
            uin=row["uin"] or "",
            key=row["key"] or "",
            pass_ticket=row["pass_ticket"] or "",
            is_default=bool(row["is_default"]),
            is_disabled=bool(row.get("is_disabled", False)),
            last_synced_at=last_synced_at,
            group_id=row.get("group_id"),
            group_name=row.get("group_name"),
        )

    @staticmethod
    def _row_to_article(row: psycopg2.extras.DictRow) -> ArticleRecord:
        raw = json.loads(row["raw_json"])
        return ArticleRecord(
            biz=row["biz"],
            article_id=row["article_id"],
            title=row["title"],
            author=row["author"],
            digest=row["digest"],
            cover=row["cover"],
            link=row["link"],
            source_url=row["source_url"],
            publish_at=row["publish_at"],
            raw=raw,
        )


def open_storage(db_path: Path | str = DB_PATH, *, auto_init: bool = False) -> StorageLike:
    dsn = os.environ.get("HIPPO_PG_DSN")
    if dsn:
        return PostgresStorage(dsn, auto_init=auto_init)
    return Storage(db_path, auto_init=auto_init)
