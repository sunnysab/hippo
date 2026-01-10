"""SQLite-backed persistence for the CLI."""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

from .config import DB_PATH
from .models import AccountCredential, ArticleRecord, LoginSession

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
SCHEMA_VERSION = "2"


def _utc_now() -> str:
    return datetime.utcnow().strftime(ISO_FORMAT)


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
                link TEXT NOT NULL UNIQUE,
                source_url TEXT,
                publish_at INTEGER,
                raw_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
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
        cur.execute(
            """
            INSERT OR REPLACE INTO meta(key, value)
            VALUES ('schema_version', ?)
            """,
            (SCHEMA_VERSION,)
        )
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
        if not row:
            raise LookupError("No account found. Create one with `accounts add`." )
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
                    ON CONFLICT(link) DO UPDATE SET
                        title=excluded.title,
                        author=excluded.author,
                        digest=excluded.digest,
                        cover=excluded.cover,
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
