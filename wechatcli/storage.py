"""SQLite-backed persistence for the CLI."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Protocol

import psycopg2
import psycopg2.extras

from .config import DB_PATH
from .models import AccountCredential, ArticleRecord, LoginSession

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
SCHEMA_VERSION = "3"


def _utc_now() -> str:
    return datetime.utcnow().strftime(ISO_FORMAT)


class StorageLike(Protocol):
    def close(self) -> None: ...
    def get_meta(self, key: str) -> Optional[str]: ...
    def set_meta(self, key: str, value: str) -> None: ...
    def delete_meta(self, key: str) -> None: ...
    def upsert_account(self, account: AccountCredential) -> AccountCredential: ...
    def list_accounts(self) -> List[AccountCredential]: ...
    def get_account(
        self, biz: Optional[str] = None, *, fallback_to_default: bool = True
    ) -> AccountCredential: ...
    def remove_account(self, biz: str) -> int: ...
    def set_default_account(self, biz: str) -> None: ...
    def update_last_synced(self, biz: str) -> None: ...
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

    def __init__(self, db_path: Path | str = DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

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
            CREATE TABLE IF NOT EXISTS accounts (
                biz TEXT PRIMARY KEY,
                nickname TEXT NOT NULL,
                alias TEXT,
                round_head_img TEXT,
                uin TEXT NOT NULL,
                key TEXT NOT NULL,
                pass_ticket TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                last_synced_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
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
                                  is_default, last_synced_at, created_at, updated_at)
            VALUES (:biz, :nickname, :alias, :round_head_img, :uin, :key, :pass_ticket,
                    :is_default, :last_synced_at, :created_at, :updated_at)
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
                "is_default": 1 if account.is_default else 0,
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

    def list_accounts(self) -> List[AccountCredential]:
        rows = self.conn.execute(
            "SELECT * FROM accounts ORDER BY is_default DESC, nickname ASC"
        ).fetchall()
        return [self._row_to_account(row) for row in rows]

    def get_account(
        self, biz: Optional[str] = None, *, fallback_to_default: bool = True
    ) -> AccountCredential:
        row = None
        if biz:
            row = self.conn.execute("SELECT * FROM accounts WHERE biz = ?", (biz,)).fetchone()
        if not row and fallback_to_default:
            row = self.conn.execute("SELECT * FROM accounts WHERE is_default = 1 LIMIT 1").fetchone()
        if not row and fallback_to_default and not biz:
            row = self.conn.execute(
                "SELECT * FROM accounts ORDER BY updated_at DESC LIMIT 1"
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

    def update_last_synced(self, biz: str) -> None:
        self.conn.execute(
            "UPDATE accounts SET last_synced_at = ?, updated_at = ? WHERE biz = ?",
            (_utc_now(), _utc_now(), biz),
        )
        self.conn.commit()

    # Login session helpers ------------------------------------------------
    def save_login_session(self, session: LoginSession, *, set_default: bool = True) -> LoginSession:
        now = _utc_now()
        if set_default:
            self.conn.execute("UPDATE login_sessions SET is_default = 0")
        self.conn.execute(
            """
            INSERT INTO login_sessions (token, cookies_json, nickname, avatar, is_default, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.token,
                json.dumps(session.cookies, ensure_ascii=False),
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
            raise LookupError("No login session found. Run `wechatcli login` first.")
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
        limit: int = 10,
        since_timestamp: Optional[int] = None,
    ) -> List[ArticleRecord]:
        query = "SELECT * FROM articles WHERE biz = ?"
        params: list = [biz]
        if since_timestamp is not None:
            query += " AND (publish_at IS NULL OR publish_at >= ?)"
            params.append(since_timestamp)
        query += " ORDER BY publish_at IS NULL, publish_at DESC, id DESC LIMIT ?"
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
            last_synced_at=last_synced_at,
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
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = False
        self._init_db()

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
                CREATE TABLE IF NOT EXISTS accounts (
                    biz TEXT PRIMARY KEY,
                    nickname TEXT NOT NULL,
                    alias TEXT,
                    round_head_img TEXT,
                    uin TEXT NOT NULL,
                    key TEXT NOT NULL,
                    pass_ticket TEXT NOT NULL,
                    is_default BOOLEAN NOT NULL DEFAULT FALSE,
                    last_synced_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
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
                    id SERIAL PRIMARY KEY,
                    token TEXT NOT NULL,
                    cookies_json TEXT NOT NULL,
                    nickname TEXT,
                    avatar TEXT,
                    is_default BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
        now = _utc_now()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO accounts (biz, nickname, alias, round_head_img, uin, key, pass_ticket,
                                      is_default, last_synced_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    account.is_default,
                    account.last_synced_at.strftime(ISO_FORMAT) if account.last_synced_at else None,
                    now,
                    now,
                ),
            )
        self.conn.commit()
        if account.is_default:
            self.set_default_account(account.biz)
        return self.get_account(account.biz, fallback_to_default=False)

    def list_accounts(self) -> List[AccountCredential]:
        with self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM accounts ORDER BY is_default DESC, nickname ASC")
            rows = cur.fetchall()
        return [self._row_to_account(row) for row in rows]

    def get_account(
        self, biz: Optional[str] = None, *, fallback_to_default: bool = True
    ) -> AccountCredential:
        row = None
        with self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if biz:
                cur.execute("SELECT * FROM accounts WHERE biz = %s", (biz,))
                row = cur.fetchone()
            if not row and fallback_to_default:
                cur.execute("SELECT * FROM accounts WHERE is_default = TRUE LIMIT 1")
                row = cur.fetchone()
            if not row and fallback_to_default and not biz:
                cur.execute("SELECT * FROM accounts ORDER BY updated_at DESC LIMIT 1")
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

    def update_last_synced(self, biz: str) -> None:
        now = _utc_now()
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE accounts SET last_synced_at = %s, updated_at = %s WHERE biz = %s",
                (now, now, biz),
            )
        self.conn.commit()

    # Login session helpers ------------------------------------------------
    def save_login_session(self, session: LoginSession, *, set_default: bool = True) -> LoginSession:
        now = _utc_now()
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
                    json.dumps(session.cookies, ensure_ascii=False),
                    session.nickname,
                    session.avatar,
                    True if set_default else False,
                    now,
                    now,
                ),
            )
        self.conn.commit()
        return self.get_login_session()

    def get_login_session(self) -> LoginSession:
        with self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM login_sessions WHERE is_default = TRUE ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        if not row:
            raise LookupError("No login session found. Run `wechatcli login` first.")
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

    def list_articles(
        self,
        biz: str,
        *,
        limit: int = 10,
        since_timestamp: Optional[int] = None,
    ) -> List[ArticleRecord]:
        query = "SELECT * FROM articles WHERE biz = %s"
        params: list = [biz]
        if since_timestamp is not None:
            query += " AND (publish_at IS NULL OR publish_at >= %s)"
            params.append(since_timestamp)
        query += " ORDER BY publish_at IS NULL, publish_at DESC, id DESC LIMIT %s"
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
            last_synced_at=last_synced_at,
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


def open_storage(db_path: Path | str = DB_PATH) -> StorageLike:
    dsn = os.environ.get("WECHATCLI_PG_DSN")
    if dsn:
        return PostgresStorage(dsn)
    return Storage(db_path)
