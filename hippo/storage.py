"""PostgreSQL-backed persistence for the CLI."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from .env import load_env
from .models import AccountCredential, AccountGroup, ArticleRecord, LoginSession
from .s3 import build_image_key, get_s3_client, upload_object_bytes

load_env()

SCHEMA_VERSION = '12'

SCHEMA_PATH = Path(__file__).resolve().parent.parent / 'schema' / 'postgres.sql'


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


UPSERT_SCHEMA_VERSION = """
INSERT INTO meta(key, value)
VALUES ('schema_version', %s)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
"""

_PG_POOL: ConnectionPool | None = None
_PG_POOL_DSN: str | None = None
_DB_INIT_LOG_VALUES = {'1', 'true', 'yes', 'on'}


def _db_init_log_enabled() -> bool:
    return os.environ.get('HIPPO_DB_INIT_LOG', '').strip().lower() in _DB_INIT_LOG_VALUES


def _log_db_init(message: str) -> None:
    if not _db_init_log_enabled():
        return
    print(f'[db init] {message}', file=sys.stderr, flush=True)


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


def _load_schema_sql() -> str:
    try:
        return SCHEMA_PATH.read_text(encoding='utf-8')
    except FileNotFoundError as exc:
        raise StorageInitError(f'Schema file not found: {SCHEMA_PATH}') from exc


def _split_sql_statements(sql_text: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    in_single = False
    in_double = False
    dollar_tag: str | None = None
    idx = 0
    length = len(sql_text)
    while idx < length:
        char = sql_text[idx]
        if dollar_tag:
            if sql_text.startswith(dollar_tag, idx):
                buffer.append(dollar_tag)
                idx += len(dollar_tag)
                dollar_tag = None
            else:
                buffer.append(char)
                idx += 1
            continue
        if in_single:
            buffer.append(char)
            if char == "'":
                if idx + 1 < length and sql_text[idx + 1] == "'":
                    buffer.append("'")
                    idx += 2
                    continue
                in_single = False
            idx += 1
            continue
        if in_double:
            buffer.append(char)
            if char == '"':
                if idx + 1 < length and sql_text[idx + 1] == '"':
                    buffer.append('"')
                    idx += 2
                    continue
                in_double = False
            idx += 1
            continue
        if char == "'":
            in_single = True
            buffer.append(char)
            idx += 1
            continue
        if char == '"':
            in_double = True
            buffer.append(char)
            idx += 1
            continue
        if char == '$':
            tag_end = idx + 1
            while tag_end < length and (
                sql_text[tag_end].isalnum() or sql_text[tag_end] == '_'
            ):
                tag_end += 1
            if tag_end < length and sql_text[tag_end] == '$':
                dollar_tag = sql_text[idx : tag_end + 1]
                buffer.append(dollar_tag)
                idx = tag_end + 1
                continue
        if char == ';':
            statement = ''.join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            idx += 1
            continue
        buffer.append(char)
        idx += 1
    statement = ''.join(buffer).strip()
    if statement:
        statements.append(statement)
    return statements


def _utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _session_identity(cookies: dict[str, str]) -> str | None:
    for key in ("wxuin", "uin", "fakeuin", "mpuin"):
        value = cookies.get(key)
        if value:
            return f"{key}:{value}"
    return None


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
            schema_sql = _load_schema_sql()
            statements = _split_sql_statements(schema_sql)
            total = len(statements)
            for idx, statement in enumerate(statements, start=1):
                first_line = statement.strip().splitlines()[0].strip()
                preview = first_line[:160]
                if len(first_line) > 160:
                    preview = f'{preview}...'
                _log_db_init(f'sql {idx}/{total}: {preview}')
                cur.execute(statement)
            cur.execute(ARTICLES_COLUMN_CHECK_QUERY)
            existing_columns = {row[0] for row in cur.fetchall()}
            if {
                "url_token",
                "clean_html",
                "content_markdown",
                "content_json",
            }.issubset(existing_columns):
                _log_db_init('migrate article_content')
                cur.execute(ARTICLE_CONTENT_MIGRATION_QUERY)
            _log_db_init('drop legacy article columns')
            cur.execute(DROP_ARTICLE_LEGACY_COLUMNS)
            _log_db_init('update schema version')
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
                            (article_pk, position, kind, orig_url, content_type, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            article_pk,
                            image.get("position", 0),
                            image.get("kind", "inline"),
                            orig_url,
                            image.get("content_type"),
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
        bundle = get_s3_client()
        if not bundle:
            raise StorageInitError(
                'Missing S3 config. Set HIPPO_S3_ENDPOINT/HIPPO_S3_BUCKET/HIPPO_S3_ACCESS_KEY/HIPPO_S3_SECRET_KEY.'
            )
        config, client = bundle
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
                    "SELECT id, s3_key FROM article_images WHERE article_pk = %s AND orig_url = %s",
                    (article_pk, orig_url),
                )
                image_row = cur.fetchone()
                if not image_row:
                    return
                image_id, existing_key = image_row
            s3_key = existing_key or build_image_key(config.prefix, image_id, content_type)
            upload_object_bytes(
                client,
                bucket=config.bucket,
                key=s3_key,
                payload=data,
                content_type=content_type,
            )
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE article_images
                    SET content_type = %s,
                        s3_key = %s,
                        failed_at = NULL,
                        failed_reason = NULL,
                        updated_at = %s
                    WHERE article_pk = %s AND orig_url = %s
                    """,
                    (
                        content_type,
                        s3_key,
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
        exclude_downloaded: bool = False,
    ) -> list[ArticleRecord]:
        query_parts = ["SELECT a.* FROM articles a"]
        params: list = []

        if exclude_downloaded:
            query_parts.append("LEFT JOIN article_content c ON c.article_pk = a.id")
        
        query_parts.append("WHERE a.biz = %s")
        params.append(biz)

        if exclude_downloaded:
            query_parts.append("AND c.id IS NULL")

        if since_timestamp is not None:
            query_parts.append("AND (a.publish_at IS NULL OR a.publish_at >= %s)")
            params.append(since_timestamp)
        
        query_parts.append("ORDER BY a.publish_at IS NULL, a.publish_at DESC, a.id DESC")
        
        if limit is not None:
            query_parts.append("LIMIT %s")
            params.append(limit)
            
        query = "\n".join(query_parts)
        
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


def open_storage(*, auto_init: bool = False) -> PostgresStorage:
    load_env()
    dsn = os.environ.get("HIPPO_PG_DSN")
    if not dsn:
        raise StorageInitError("Missing HIPPO_PG_DSN for PostgreSQL storage.")
    return PostgresStorage(dsn, auto_init=auto_init)
