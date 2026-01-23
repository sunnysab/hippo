"""PostgreSQL-backed persistence for the CLI."""

from __future__ import annotations

import json
import os
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from .models import AccountCredential, AccountGroup, ArticleRecord, LoginSession

SCHEMA_VERSION = '10'

SCHEMA_INIT_STATEMENTS = [
    """
    CREATE EXTENSION IF NOT EXISTS pg_jieba
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS account_groups (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        sync_mode TEXT,
        sync_recent_days INTEGER,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS accounts (
        biz TEXT PRIMARY KEY,
        nickname TEXT NOT NULL,
        alias TEXT,
        round_head_img TEXT,
        group_id INTEGER REFERENCES account_groups(id) ON DELETE SET NULL,
        is_disabled BOOLEAN NOT NULL DEFAULT FALSE,
        sync_mode TEXT,
        sync_recent_days INTEGER,
        last_synced_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS is_disabled BOOLEAN NOT NULL DEFAULT FALSE
    """,
    """
    ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS group_id INTEGER REFERENCES account_groups(id) ON DELETE SET NULL
    """,
    """
    ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS sync_mode TEXT
    """,
    """
    ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS sync_recent_days INTEGER
    """,
    """
    ALTER TABLE accounts
    DROP COLUMN IF EXISTS is_default
    """,
    """
    ALTER TABLE accounts
    DROP COLUMN IF EXISTS uin,
    DROP COLUMN IF EXISTS key,
    DROP COLUMN IF EXISTS pass_ticket
    """,
    """
    ALTER TABLE account_groups
    ADD COLUMN IF NOT EXISTS sync_mode TEXT
    """,
    """
    ALTER TABLE account_groups
    ADD COLUMN IF NOT EXISTS sync_recent_days INTEGER
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_accounts_group
    ON accounts (group_id)
    """,
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
    """,
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
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_article_content_article_pk
    ON article_content (article_pk)
    """,
    """
    ALTER TABLE articles
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    """,
    """
    CREATE OR REPLACE FUNCTION build_article_search_vector(
        title TEXT,
        author TEXT,
        digest TEXT,
        content TEXT
    ) RETURNS tsvector AS $$
    SELECT
        setweight(to_tsvector('jiebaqry', COALESCE(title, '')), 'A') ||
        setweight(to_tsvector('jiebaqry', COALESCE(author, '')), 'C') ||
        setweight(to_tsvector('jiebaqry', COALESCE(digest, '')), 'B') ||
        setweight(
            to_tsvector('jiebaqry', COALESCE(SUBSTRING(content FROM 1 FOR 50000), '')),
            'B'
        );
    $$ LANGUAGE sql STABLE
    """,
    """
    CREATE OR REPLACE FUNCTION articles_search_vector_trigger()
    RETURNS trigger AS $$
    DECLARE
        content_text TEXT;
    BEGIN
        IF TG_OP = 'INSERT' AND NEW.id IS NULL THEN
            content_text := '';
        ELSE
            SELECT COALESCE(c.content_markdown, c.clean_html, '')
            INTO content_text
            FROM article_content c
            WHERE c.article_pk = NEW.id;
        END IF;
        NEW.search_vector = build_article_search_vector(
            NEW.title,
            NEW.author,
            NEW.digest,
            content_text
        );
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    DROP TRIGGER IF EXISTS trg_articles_search_vector ON articles
    """,
    """
    CREATE TRIGGER trg_articles_search_vector
    BEFORE INSERT OR UPDATE OF title, author, digest
    ON articles
    FOR EACH ROW EXECUTE FUNCTION articles_search_vector_trigger()
    """,
    """
    CREATE OR REPLACE FUNCTION article_content_search_vector_trigger()
    RETURNS trigger AS $$
    BEGIN
        UPDATE articles
        SET search_vector = build_article_search_vector(
            title,
            author,
            digest,
            COALESCE(NEW.content_markdown, NEW.clean_html, '')
        )
        WHERE id = NEW.article_pk;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    DROP TRIGGER IF EXISTS trg_article_content_search_vector ON article_content
    """,
    """
    CREATE TRIGGER trg_article_content_search_vector
    AFTER INSERT OR UPDATE OF content_markdown, clean_html
    ON article_content
    FOR EACH ROW EXECUTE FUNCTION article_content_search_vector_trigger()
    """,
    """
    UPDATE articles a
    SET search_vector = build_article_search_vector(
        a.title,
        a.author,
        a.digest,
        COALESCE(c.content_markdown, c.clean_html, '')
    )
    FROM article_content c
    WHERE c.article_pk = a.id
    """,
    """
    UPDATE articles
    SET search_vector = build_article_search_vector(title, author, digest, '')
    WHERE search_vector IS NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_articles_search_vector
    ON articles USING GIN (search_vector)
    """,
]


ARTICLES_COLUMN_CHECK_QUERY = """
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


ARTICLE_CONTENT_MIGRATION_QUERY = """
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


DROP_ARTICLE_LEGACY_COLUMNS = """
ALTER TABLE articles
DROP COLUMN IF EXISTS url_token,
DROP COLUMN IF EXISTS clean_html,
DROP COLUMN IF EXISTS content_markdown,
DROP COLUMN IF EXISTS content_json,
DROP COLUMN IF EXISTS cover_image_id
"""


CREATE_ARTICLES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_articles_biz_publish
ON articles (biz, publish_at DESC)
"""


CREATE_ARTICLE_IMAGES_TABLE = """
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


ALTER_ARTICLE_IMAGES_TABLE = """
ALTER TABLE article_images
ADD COLUMN IF NOT EXISTS failed_at TIMESTAMPTZ,
ADD COLUMN IF NOT EXISTS failed_reason TEXT
"""


CREATE_LOGIN_SESSIONS_TABLE = """
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


UPSERT_SCHEMA_VERSION = """
INSERT INTO meta(key, value)
VALUES ('schema_version', %s)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
"""

_PG_POOL: ConnectionPool | None = None
_PG_POOL_DSN: str | None = None


def _get_pool(dsn: str) -> ConnectionPool:
    global _PG_POOL, _PG_POOL_DSN
    if _PG_POOL is not None and _PG_POOL_DSN == dsn:
        return _PG_POOL
    if _PG_POOL is not None:
        _PG_POOL.close()
    min_conn = int(os.environ.get("HIPPO_PG_POOL_MIN", "1") or "1")
    max_conn = int(os.environ.get("HIPPO_PG_POOL_MAX", "8") or "8")
    if max_conn < min_conn:
        max_conn = min_conn
    _PG_POOL = ConnectionPool(
        conninfo=dsn,
        min_size=min_conn,
        max_size=max_conn,
        kwargs={"options": "-c timezone=Asia/Shanghai"},
    )
    _PG_POOL_DSN = dsn
    return _PG_POOL


class StorageInitError(RuntimeError):
    pass


def _utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _session_identity(cookies: dict[str, str]) -> str | None:
    for key in ("wxuin", "uin", "fakeuin", "mpuin"):
        value = cookies.get(key)
        if value:
            return f"{key}:{value}"
    return None


class StorageLike(Protocol):
    def close(self) -> None: ...
    def get_meta(self, key: str) -> str | None: ...
    def set_meta(self, key: str, value: str) -> None: ...
    def delete_meta(self, key: str) -> None: ...
    def upsert_account(self, account: AccountCredential) -> AccountCredential: ...
    def list_accounts(self, group: str | None = None) -> list[AccountCredential]: ...
    def get_account(
        self, biz: str | None = None, *, fallback_to_default: bool = True
    ) -> AccountCredential: ...
    def remove_account(self, biz: str) -> int: ...
    def upsert_group(self, name: str) -> AccountGroup: ...
    def list_groups(self) -> list[AccountGroup]: ...
    def set_account_group(self, biz: str, group_name: str | None) -> None: ...
    def update_last_synced(self, biz: str) -> None: ...
    def set_account_disabled(self, biz: str, is_disabled: bool) -> None: ...
    def save_login_session(
        self, session: LoginSession, *, set_default: bool = True
    ) -> LoginSession: ...
    def get_login_session(self) -> LoginSession: ...
    def save_articles(self, articles: Iterable[ArticleRecord]) -> int: ...
    def list_articles(
        self, biz: str, *, limit: int = 10, since_timestamp: int | None = None
    ) -> list[ArticleRecord]: ...
    def get_existing_article_ids(self, biz: str, article_ids: Iterable[str]) -> set[str]: ...


class PostgresStorage(AbstractContextManager):
    def __init__(
        self,
        dsn: str,
        *,
        auto_init: bool = False,
        pool: ConnectionPool | None = None,
    ) -> None:
        self.dsn = dsn
        self._pool = pool or _get_pool(dsn)
        self.conn = self._pool.getconn()
        self.conn.autocommit = False
        if auto_init:
            self._init_db()
        else:
            try:
                self._ensure_initialized()
            except Exception:
                self.close()
                raise

    def __enter__(self) -> PostgresStorage:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        self.close()

    def close(self) -> None:
        if self._pool:
            try:
                self.conn.rollback()
            except Exception:
                pass
            self._pool.putconn(self.conn)
        else:
            self.conn.close()

    def _init_db(self) -> None:
        with self.conn.cursor() as cur:
            for statement in SCHEMA_INIT_STATEMENTS:
                cur.execute(statement)
            cur.execute(ARTICLES_COLUMN_CHECK_QUERY)
            existing_columns = {row[0] for row in cur.fetchall()}
            if {
                "url_token",
                "clean_html",
                "content_markdown",
                "content_json",
            }.issubset(existing_columns):
                cur.execute(ARTICLE_CONTENT_MIGRATION_QUERY)
            cur.execute(DROP_ARTICLE_LEGACY_COLUMNS)
            cur.execute(CREATE_ARTICLES_INDEX)
            cur.execute(CREATE_ARTICLE_IMAGES_TABLE)
            cur.execute(ALTER_ARTICLE_IMAGES_TABLE)
            cur.execute(CREATE_LOGIN_SESSIONS_TABLE)
            cur.execute(UPSERT_SCHEMA_VERSION, (SCHEMA_VERSION,))
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
    def get_meta(self, key: str) -> str | None:
        with self.conn.cursor(row_factory=dict_row) as cur:
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
                INSERT INTO accounts (biz, nickname, alias, round_head_img,
                                      group_id, is_disabled, sync_mode, sync_recent_days,
                                      last_synced_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (biz) DO UPDATE SET
                    nickname=EXCLUDED.nickname,
                    alias=EXCLUDED.alias,
                    round_head_img=EXCLUDED.round_head_img,
                    updated_at=EXCLUDED.updated_at
                """,
                (
                    account.biz,
                    account.nickname,
                    account.alias,
                    account.round_head_img,
                    account.group_id,
                    account.is_disabled,
                    account.sync_mode,
                    account.sync_recent_days,
                    account.last_synced_at,
                    now,
                    now,
                ),
            )
        self.conn.commit()
        return self.get_account(account.biz, fallback_to_default=False)

    def list_accounts(self, group: str | None = None) -> list[AccountCredential]:
        query = """
            SELECT a.*, g.name AS group_name
            FROM accounts a
            LEFT JOIN account_groups g ON g.id = a.group_id
        """
        params: list = []
        if group:
            query += " WHERE g.name = %s"
            params.append(group)
        query += " ORDER BY a.nickname ASC"
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return [self._row_to_account(row) for row in rows]

    def get_account(
        self, biz: str | None = None, *, fallback_to_default: bool = True
    ) -> AccountCredential:
        row = None
        with self.conn.cursor(row_factory=dict_row) as cur:
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

    def upsert_group(self, name: str) -> AccountGroup:
        trimmed = name.strip()
        if not trimmed:
            raise ValueError("Group name cannot be empty.")
        now = _utc_now_dt()
        with self.conn.cursor(row_factory=dict_row) as cur:
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

    def list_groups(self) -> list[AccountGroup]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT g.id, g.name, g.sync_mode, g.sync_recent_days, COUNT(a.biz) AS account_count
                FROM account_groups g
                LEFT JOIN accounts a ON a.group_id = g.id
                GROUP BY g.id, g.name, g.sync_mode, g.sync_recent_days
                ORDER BY g.name ASC
                """
            )
            rows = cur.fetchall()
        return [
            AccountGroup(
                id=row["id"],
                name=row["name"],
                account_count=row["account_count"],
                sync_mode=row.get('sync_mode'),
                sync_recent_days=row.get('sync_recent_days'),
            )
            for row in rows
        ]

    def set_account_group(self, biz: str, group_name: str | None) -> None:
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
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id, cookies_json, nickname FROM login_sessions ORDER BY id DESC")
            rows = cur.fetchall()
        match_id: int | None = None
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
            except psycopg.errors.UniqueViolation:
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
        with self.conn.cursor(row_factory=dict_row) as cur:
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
        url_token: str | None,
        title: str,
        clean_html: str,
        content_markdown: str,
        content_blocks: list[dict],
        cover_url: str | None,
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
                            psycopg.Binary(image.get("data")) if image.get("data") else None,
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
                        Json(updated_blocks),
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
        content_type: str | None,
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
                        psycopg.Binary(data) if data else None,
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
        limit: int | None = 10,
        since_timestamp: int | None = None,
    ) -> list[ArticleRecord]:
        query = "SELECT * FROM articles WHERE biz = %s"
        params: list = [biz]
        if since_timestamp is not None:
            query += " AND (publish_at IS NULL OR publish_at >= %s)"
            params.append(since_timestamp)
        query += " ORDER BY publish_at IS NULL, publish_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        with self.conn.cursor(row_factory=dict_row) as cur:
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
    def _row_to_account(row: dict[str, Any]) -> AccountCredential:
        last_synced_at = row["last_synced_at"] if row["last_synced_at"] else None
        return AccountCredential(
            biz=row["biz"],
            nickname=row["nickname"],
            alias=row["alias"],
            round_head_img=row["round_head_img"],
            is_disabled=bool(row.get("is_disabled", False)),
            last_synced_at=last_synced_at,
            sync_mode=row.get('sync_mode'),
            sync_recent_days=row.get('sync_recent_days'),
            group_id=row.get("group_id"),
            group_name=row.get("group_name"),
        )

    @staticmethod
    def _row_to_article(row: dict[str, Any]) -> ArticleRecord:
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


def open_storage(*, auto_init: bool = False) -> StorageLike:
    dsn = os.environ.get("HIPPO_PG_DSN")
    if not dsn:
        raise StorageInitError("Missing HIPPO_PG_DSN for PostgreSQL storage.")
    return PostgresStorage(dsn, auto_init=auto_init)
