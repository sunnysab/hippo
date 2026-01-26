"""Repository layer for Postgres storage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from .models import AccountCredential, AccountGroup, ArticleRecord, LoginSession


@dataclass(frozen=True)
class ArticleImageTarget:
    article_pk: int
    image_id: int
    s3_key: str | None


def _utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _session_identity(cookies: dict[str, str]) -> str | None:
    for key in ('wxuin', 'uin', 'fakeuin', 'mpuin'):
        value = cookies.get(key)
        if value:
            return f'{key}:{value}'
    return None


class MetaRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def get(self, key: str) -> str | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute('SELECT value FROM meta WHERE key = %s', (key,))
            row = cur.fetchone()
            return row['value'] if row else None

    def set(self, key: str, value: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                'INSERT INTO meta(key, value) VALUES (%s, %s) '
                'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value',
                (key, value),
            )

    def delete(self, key: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute('DELETE FROM meta WHERE key = %s', (key,))


class AccountRepository:
    def __init__(self, conn: psycopg.Connection, *, group_repo: 'GroupRepository | None' = None) -> None:
        self._conn = conn
        self._group_repo = group_repo

    def upsert_account(self, account: AccountCredential) -> AccountCredential:
        now = _utc_now_dt()
        with self._conn.cursor() as cur:
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
        return self.get_account(account.biz, fallback_to_default=False)

    def list_accounts(self, *, group: str | None = None) -> list[AccountCredential]:
        query = (
            'SELECT a.*, g.name AS group_name '\
            'FROM accounts a '\
            'LEFT JOIN account_groups g ON g.id = a.group_id'
        )
        params: list = []
        if group:
            query += ' WHERE g.name = %s'
            params.append(group)
        query += ' ORDER BY a.nickname ASC'
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return [_row_to_account(row) for row in rows]

    def get_account(self, biz: str | None = None, *, fallback_to_default: bool = True) -> AccountCredential:
        row = None
        with self._conn.cursor(row_factory=dict_row) as cur:
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
            raise LookupError('No account found. Create one with `accounts add` or `accounts search --interactive`.')
        return _row_to_account(row)

    def remove_account(self, biz: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute('DELETE FROM accounts WHERE biz = %s', (biz,))
            removed = cur.rowcount
        return removed

    def set_account_group(self, biz: str, group_name: str | None) -> None:
        if self._group_repo is None:
            raise RuntimeError('Group repository not configured.')
        target_name = group_name.strip() if group_name else ''
        now = _utc_now_dt()
        with self._conn.cursor() as cur:
            if not target_name:
                cur.execute(
                    'UPDATE accounts SET group_id = NULL, updated_at = %s WHERE biz = %s',
                    (now, biz),
                )
                updated = cur.rowcount
                if updated == 0:
                    raise LookupError(f'Account {biz} not found')
                return
        group = self._group_repo.upsert_group(target_name)
        with self._conn.cursor() as cur:
            cur.execute(
                'UPDATE accounts SET group_id = %s, updated_at = %s WHERE biz = %s',
                (group.id, now, biz),
            )
            updated = cur.rowcount
        if updated == 0:
            raise LookupError(f'Account {biz} not found')

    def update_last_synced(self, biz: str) -> None:
        now = _utc_now_dt()
        with self._conn.cursor() as cur:
            cur.execute(
                'UPDATE accounts SET last_synced_at = %s, updated_at = %s WHERE biz = %s',
                (now, now, biz),
            )

    def set_account_disabled(self, biz: str, is_disabled: bool) -> None:
        now = _utc_now_dt()
        with self._conn.cursor() as cur:
            cur.execute(
                'UPDATE accounts SET is_disabled = %s, updated_at = %s WHERE biz = %s',
                (is_disabled, now, biz),
            )
            updated = cur.rowcount
        if updated == 0:
            raise LookupError(f'Account {biz} not found')


class GroupRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def upsert_group(self, name: str) -> AccountGroup:
        trimmed = name.strip()
        if not trimmed:
            raise ValueError('Group name cannot be empty.')
        now = _utc_now_dt()
        with self._conn.cursor(row_factory=dict_row) as cur:
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
        if not row:
            raise RuntimeError(f'Failed to create group {trimmed}.')
        return AccountGroup(id=row['id'], name=row['name'])

    def list_groups(self) -> list[AccountGroup]:
        with self._conn.cursor(row_factory=dict_row) as cur:
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
                id=row['id'],
                name=row['name'],
                account_count=row['account_count'],
                sync_mode=row.get('sync_mode'),
                sync_recent_days=row.get('sync_recent_days'),
            )
            for row in rows
        ]


class LoginSessionRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def save_login_session(self, session: LoginSession, *, set_default: bool = True) -> LoginSession:
        now = _utc_now_dt()
        cookie_json = json.dumps(session.cookies, ensure_ascii=False)
        session_identity = _session_identity(session.cookies)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute('SELECT id, cookies_json, nickname FROM login_sessions ORDER BY id DESC')
            rows = cur.fetchall()
        match_id: int | None = None
        if session_identity:
            for row in rows:
                try:
                    row_cookies = json.loads(row['cookies_json'])
                except Exception:
                    continue
                if _session_identity(row_cookies) == session_identity:
                    match_id = row['id']
                    break
        with self._conn.cursor(row_factory=dict_row) as cur:
            if match_id is not None:
                cur.execute(
                    """
                    UPDATE login_sessions
                    SET token = %s,
                        cookies_json = %s,
                        nickname = %s,
                        avatar = %s,
                        is_default = %s,
                        updated_at = %s
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
            else:
                cur.execute(
                    """
                    INSERT INTO login_sessions
                        (token, cookies_json, nickname, avatar, is_default, created_at, updated_at)
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
        return self.get_login_session()

    def reset_login_session_sequence(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT setval(
                    pg_get_serial_sequence('login_sessions', 'id'),
                    COALESCE((SELECT MAX(id) FROM login_sessions), 1),
                    (SELECT COUNT(*) FROM login_sessions) > 0
                )
                """
            )

    def get_login_session(self) -> LoginSession:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute('SELECT * FROM login_sessions WHERE is_default = TRUE ORDER BY id DESC LIMIT 1')
            row = cur.fetchone()
        if not row:
            raise LookupError('No login session found. Run `hippo login` first.')
        cookies = json.loads(row['cookies_json'])
        return LoginSession(
            token=row['token'],
            cookies=cookies,
            nickname=row['nickname'],
            avatar=row['avatar'],
        )

    def get_login_updated_at(self) -> datetime | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                'SELECT updated_at FROM login_sessions WHERE is_default = TRUE ORDER BY id DESC LIMIT 1'
            )
            row = cur.fetchone()
        if not row:
            return None
        updated_at = row.get('updated_at')
        if isinstance(updated_at, str):
            try:
                parsed = datetime.fromisoformat(updated_at)
            except ValueError:
                return None
            return _to_utc_dt(parsed)
        if isinstance(updated_at, datetime):
            return _to_utc_dt(updated_at)
        return None


class ArticleRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def _ensure_cover_image(
        self,
        cur: psycopg.Cursor,
        *,
        article_pk: int,
        cover_url: str | None,
        now: datetime,
    ) -> int | None:
        if not cover_url:
            return None
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
            if kind != 'cover':
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
            (article_pk, 0, 'cover', cover_url, None, now),
        )
        return int(cur.fetchone()[0])

    def _normalize_cover_id(self, cover: str | int | None) -> int | None:
        if cover is None:
            return None
        if isinstance(cover, int):
            return cover
        if isinstance(cover, str) and cover.isdigit():
            return int(cover)
        return None

    def save_articles(self, articles: Iterable[ArticleRecord]) -> int:
        now = _utc_now_dt()
        inserted = 0
        with self._conn.cursor() as cur:
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
                        article.title,
                        article.author,
                        article.digest,
                        None,
                        article.link,
                        article.source_url,
                        article.publish_at,
                        json.dumps(article.raw, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                article_pk = int(cur.fetchone()[0])
                cover_id = self._normalize_cover_id(article.cover)
                if cover_id is None:
                    cover_id = self._ensure_cover_image(
                        cur,
                        article_pk=article_pk,
                        cover_url=str(article.cover) if article.cover else None,
                        now=now,
                    )
                if cover_id is not None:
                    cur.execute(
                        "UPDATE articles SET cover = %s, updated_at = %s WHERE id = %s",
                        (cover_id, now, article_pk),
                    )
                inserted += 1
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
        normalized_cover = cover_url.strip() if cover_url else None
        if normalized_cover:
            has_cover = any(
                image.get('kind') == 'cover' and str(image.get('orig_url') or '') == normalized_cover
                for image in images
            )
            if not has_cover:
                images = [
                    {
                        'orig_url': normalized_cover,
                        'kind': 'cover',
                        'position': 0,
                        'content_type': None,
                        'data': None,
                    },
                    *images,
                ]
        try:
            with self._conn.cursor() as cur:
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
                        None,
                        article.link,
                        article.source_url,
                        article.publish_at,
                        json.dumps(article.raw, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                article_pk = cur.fetchone()[0]

                cur.execute('DELETE FROM article_images WHERE article_pk = %s', (article_pk,))
                image_id_map: dict[str, int] = {}
                seen_orig_urls: set[str] = set()
                cover_id: int | None = None
                for image in images:
                    orig_url = image.get('orig_url')
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
                            image.get('position', 0),
                            image.get('kind', 'inline'),
                            orig_url,
                            image.get('content_type'),
                            now,
                        ),
                    )
                    image_id = cur.fetchone()[0]
                    if orig_url:
                        image_id_map[orig_url] = image_id
                    if image.get('kind') == 'cover' and cover_id is None:
                        cover_id = int(image_id)

                updated_blocks: list[dict] = []
                for block in content_blocks:
                    if block.get('type') == 'image':
                        orig_url = block.get('orig_url')
                        image_id = image_id_map.get(str(orig_url)) if orig_url else None
                        updated = dict(block)
                        if image_id is not None:
                            updated['image_id'] = image_id
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
                if cover_id is not None:
                    cur.execute(
                        "UPDATE articles SET cover = %s, updated_at = %s WHERE id = %s",
                        (cover_id, now, article_pk),
                    )
                else:
                    cur.execute(
                        "UPDATE articles SET cover = NULL, updated_at = %s WHERE id = %s",
                        (now, article_pk),
                    )
        except Exception:
            raise

    def has_article_content(self, biz: str, article_id: str) -> bool:
        with self._conn.cursor() as cur:
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
        with self._conn.cursor() as cur:
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

    def list_articles(
        self,
        biz: str,
        *,
        limit: int | None = 10,
        since_timestamp: int | None = None,
        exclude_downloaded: bool = False,
    ) -> list[ArticleRecord]:
        query_parts = ['SELECT a.* FROM articles a']
        params: list = []

        if exclude_downloaded:
            query_parts.append('LEFT JOIN article_content c ON c.article_pk = a.id')

        query_parts.append('WHERE a.biz = %s')
        params.append(biz)

        if exclude_downloaded:
            query_parts.append('AND c.id IS NULL')

        if since_timestamp is not None:
            query_parts.append('AND (a.publish_at IS NULL OR a.publish_at >= %s)')
            params.append(since_timestamp)

        query_parts.append('ORDER BY a.publish_at IS NULL, a.publish_at DESC, a.id DESC')

        if limit is not None:
            query_parts.append('LIMIT %s')
            params.append(limit)

        query = '\n'.join(query_parts)

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return [_row_to_article(row) for row in rows]

    def get_existing_article_ids(self, biz: str, article_ids: Iterable[str]) -> set[str]:
        ids = [item for item in article_ids if item]
        if not ids:
            return set()
        existing: set[str] = set()
        chunk_size = 900
        with self._conn.cursor() as cur:
            for i in range(0, len(ids), chunk_size):
                chunk = ids[i : i + chunk_size]
                cur.execute(
                    'SELECT article_id FROM articles WHERE biz = %s AND article_id = ANY(%s)',
                    (biz, chunk),
                )
                existing.update(row[0] for row in cur.fetchall())
        return existing


class ImageRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def get_article_image_target(self, biz: str, article_id: str, orig_url: str) -> ArticleImageTarget | None:
        with self._conn.cursor() as cur:
            cur.execute(
                'SELECT id FROM articles WHERE biz = %s AND article_id = %s',
                (biz, article_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            article_pk = row[0]
            cur.execute(
                'SELECT id, s3_key FROM article_images WHERE article_pk = %s AND orig_url = %s',
                (article_pk, orig_url),
            )
            image_row = cur.fetchone()
            if not image_row:
                return None
            image_id, existing_key = image_row
        return ArticleImageTarget(article_pk=article_pk, image_id=image_id, s3_key=existing_key)

    def update_article_image_metadata(
        self,
        *,
        article_pk: int,
        orig_url: str,
        content_type: str | None,
        s3_key: str,
    ) -> None:
        try:
            with self._conn.cursor() as cur:
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
        except Exception:
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
            with self._conn.cursor() as cur:
                cur.execute(
                    'SELECT id FROM articles WHERE biz = %s AND article_id = %s',
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
        except Exception:
            raise


def _to_utc_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _row_to_account(row: dict[str, Any]) -> AccountCredential:
    last_synced_at = row['last_synced_at'] if row['last_synced_at'] else None
    return AccountCredential(
        biz=row['biz'],
        nickname=row['nickname'],
        alias=row['alias'],
        round_head_img=row['round_head_img'],
        is_disabled=bool(row.get('is_disabled', False)),
        last_synced_at=last_synced_at,
        sync_mode=row.get('sync_mode'),
        sync_recent_days=row.get('sync_recent_days'),
        group_id=row.get('group_id'),
        group_name=row.get('group_name'),
    )


def _row_to_article(row: dict[str, Any]) -> ArticleRecord:
    raw = json.loads(row['raw_json'])
    return ArticleRecord(
        biz=row['biz'],
        article_id=row['article_id'],
        title=row['title'],
        author=row['author'],
        digest=row['digest'],
        cover=row['cover'],
        link=row['link'],
        source_url=row['source_url'],
        publish_at=row['publish_at'],
        raw=raw,
    )


__all__ = [
    'AccountRepository',
    'ArticleImageTarget',
    'ArticleRepository',
    'GroupRepository',
    'ImageRepository',
    'LoginSessionRepository',
    'MetaRepository',
]
