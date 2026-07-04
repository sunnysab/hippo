"""Repository layer for Postgres storage."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from .models import AccountCredential, AccountGroup, ArticleRecord, LoginSession
from .utils import build_set_clause, to_utc_dt, utc_now_dt


@dataclass(frozen=True)
class ArticleImageTarget:
    article_pk: int
    image_id: int
    s3_key: str | None


def _session_identity(cookies: dict[str, str]) -> str | None:
    for key in ('wxuin', 'uin', 'fakeuin', 'mpuin'):
        value = cookies.get(key)
        if value:
            return f'{key}:{value}'
    return None


ARTICLE_CONTENT_PRESENT_SQL = """
(
    (c.clean_html IS NOT NULL AND btrim(c.clean_html) <> '')
    OR (c.content_markdown IS NOT NULL AND btrim(c.content_markdown) <> '')
    OR (c.content_json IS NOT NULL AND c.content_json::text NOT IN ('[]', 'null'))
)
"""


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
                'INSERT INTO meta(key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value',
                (key, value),
            )

    def delete(self, key: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute('DELETE FROM meta WHERE key = %s', (key,))


class AccountRepository:
    def __init__(self, conn: psycopg.Connection, *, group_repo: GroupRepository | None = None) -> None:
        self._conn = conn
        self._group_repo = group_repo

    def upsert_account(self, account: AccountCredential) -> AccountCredential:
        now = utc_now_dt()
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
        query = 'SELECT a.*, g.name AS group_name FROM accounts a LEFT JOIN account_groups g ON g.id = a.group_id'
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
        now = utc_now_dt()
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
        now = utc_now_dt()
        with self._conn.cursor() as cur:
            cur.execute(
                'UPDATE accounts SET last_synced_at = %s, updated_at = %s WHERE biz = %s',
                (now, now, biz),
            )

    def set_account_disabled(self, biz: str, is_disabled: bool) -> None:
        now = utc_now_dt()
        with self._conn.cursor() as cur:
            cur.execute(
                'UPDATE accounts SET is_disabled = %s, updated_at = %s WHERE biz = %s',
                (is_disabled, now, biz),
            )
            updated = cur.rowcount
        if updated == 0:
            raise LookupError(f'Account {biz} not found')

    def list_accounts_paginated(
        self,
        *,
        group_id: int | None = None,
        search_tokens: list[str] | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        where: list[str] = []
        params: list[Any] = []
        if group_id is not None:
            where.append('a.group_id = %s')
            params.append(group_id)
        if search_tokens:
            clauses: list[str] = []
            for term in search_tokens:
                like = f'%{term}%'
                clause = ' OR '.join(
                    [
                        'a.nickname ILIKE %s',
                        'a.alias ILIKE %s',
                        'a.biz ILIKE %s',
                    ]
                )
                clauses.append(f'({clause})')
                params.extend([like, like, like])
            where.append(' AND '.join(clauses))
        where_sql = f'WHERE {" AND ".join(where)}' if where else ''
        offset = max(page - 1, 0) * page_size
        query_sql = (
            'WITH filtered_accounts AS ('
            ' SELECT a.biz, a.nickname, a.alias, a.round_head_img, a.group_id,'
            ' a.is_disabled, a.last_synced_at, a.sync_mode, a.sync_recent_days,'
            ' a.article_count'
            ' FROM accounts a'
            f' {where_sql}'
            ' ORDER BY a.nickname ASC'
            ' LIMIT %s OFFSET %s'
            ')'
            ' SELECT a.biz, a.nickname, a.alias, a.round_head_img, a.group_id,'
            ' a.is_disabled, a.last_synced_at, a.sync_mode, a.sync_recent_days, g.name AS group_name,'
            ' COALESCE(a.article_count, 0) AS article_count,'
            ' (ai.data IS NOT NULL) AS avatar_ready'
            ' FROM filtered_accounts a'
            ' LEFT JOIN account_groups g ON g.id = a.group_id'
            ' LEFT JOIN avatar_images ai ON ai.biz = a.biz'
            ' ORDER BY a.nickname ASC'
        )
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query_sql, [*params, page_size, offset])
            rows = [dict(row) for row in cur.fetchall()]
        for row in rows:
            row['avatar_url'] = f'/api/account/{row["biz"]}/avatar'
        count_sql = f'SELECT COUNT(*) AS total FROM accounts a {where_sql}'
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(count_sql, params)
            total_row = cur.fetchone()
        total = int(total_row['total']) if total_row else 0
        return {'accounts': rows, 'page': page, 'page_size': page_size, 'total': total}

    def get_account_detail(self, biz: str) -> dict[str, Any]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                'SELECT a.biz, a.nickname, a.alias, a.round_head_img, a.group_id,'
                ' a.is_disabled, a.last_synced_at, a.sync_mode, a.sync_recent_days, g.name AS group_name,'
                ' COALESCE(a.article_count, 0) AS article_count,'
                ' (ai.data IS NOT NULL) AS avatar_ready'
                ' FROM accounts a'
                ' LEFT JOIN account_groups g ON g.id = a.group_id'
                ' LEFT JOIN avatar_images ai ON ai.biz = a.biz'
                ' WHERE a.biz = %s',
                (biz,),
            )
            row = cur.fetchone()
        if not row:
            raise LookupError('Account not found')
        result = dict(row)
        result['avatar_url'] = f'/api/account/{result["biz"]}/avatar'
        return result

    def update_account_fields(self, biz: str, **updates: Any) -> None:
        fields, params = build_set_clause(
            {
                'nickname': 'nickname',
                'alias': 'alias',
                'round_head_img': 'round_head_img',
                'group_id': 'group_id',
                'is_disabled': 'is_disabled',
                'sync_mode': 'sync_mode',
                'sync_recent_days': 'sync_recent_days',
            },
            updates,
        )
        if not fields:
            raise ValueError('No fields to update')
        fields.append('updated_at = NOW()')
        params.append(biz)
        with self._conn.cursor() as cur:
            cur.execute(
                f'UPDATE accounts SET {", ".join(fields)} WHERE biz = %s',
                params,
            )
            if cur.rowcount == 0:
                raise LookupError('Account not found')

    def move_accounts(self, biz_list: list[str], group_id: int) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                'UPDATE accounts SET group_id = %s, updated_at = NOW() WHERE biz = ANY(%s)',
                (group_id, biz_list),
            )
            return cur.rowcount

    def batch_update_fields(self, biz_list: list[str], **updates: Any) -> int:
        fields, params = build_set_clause(
            {
                'sync_mode': 'sync_mode',
                'sync_recent_days': 'sync_recent_days',
            },
            updates,
        )
        if not fields:
            raise ValueError('No fields to update')
        fields.append('updated_at = NOW()')
        params.append(biz_list)
        with self._conn.cursor() as cur:
            cur.execute(
                f'UPDATE accounts SET {", ".join(fields)} WHERE biz = ANY(%s)',
                params,
            )
            return cur.rowcount


class GroupRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def get_group(self, group_id: int) -> AccountGroup:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                    g.id,
                    g.name,
                    g.sync_mode,
                    g.sync_recent_days,
                    COALESCE(g.article_count, 0) AS article_count,
                    COUNT(a.biz) AS account_count
                FROM account_groups g
                LEFT JOIN accounts a ON a.group_id = g.id
                WHERE g.id = %s
                GROUP BY g.id, g.name, g.sync_mode, g.sync_recent_days, g.article_count
                """,
                (group_id,),
            )
            row = cur.fetchone()
        if not row:
            raise LookupError('Group not found')
        return AccountGroup(
            id=row['id'],
            name=row['name'],
            account_count=row['account_count'],
            article_count=row.get('article_count') or 0,
            sync_mode=row.get('sync_mode'),
            sync_recent_days=row.get('sync_recent_days'),
        )

    def update_group(self, group_id: int, **updates: Any) -> AccountGroup:
        fields, params = build_set_clause(
            {
                'name': 'name',
                'sync_mode': 'sync_mode',
                'sync_recent_days': 'sync_recent_days',
            },
            updates,
        )
        if not fields:
            raise ValueError('No fields to update')
        fields.append('updated_at = NOW()')
        params.append(group_id)
        with self._conn.cursor() as cur:
            cur.execute(
                f'UPDATE account_groups SET {", ".join(fields)} WHERE id = %s',
                params,
            )
            if cur.rowcount == 0:
                raise LookupError('Group not found')
        return self.get_group(group_id)

    def delete_group(self, group_id: int, default_group_id: int) -> None:
        if group_id == default_group_id:
            raise ValueError('Default group cannot be deleted')
        with self._conn.cursor() as cur:
            cur.execute(
                'UPDATE accounts SET group_id = %s WHERE group_id = %s',
                (default_group_id, group_id),
            )
            cur.execute('DELETE FROM account_groups WHERE id = %s', (group_id,))
            if cur.rowcount == 0:
                raise LookupError('Group not found')

    def upsert_group(self, name: str) -> AccountGroup:
        trimmed = name.strip()
        if not trimmed:
            raise ValueError('Group name cannot be empty.')
        now = utc_now_dt()
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
                SELECT
                    g.id,
                    g.name,
                    g.sync_mode,
                    g.sync_recent_days,
                    COUNT(a.biz) AS account_count,
                    COALESCE(g.article_count, 0) AS article_count
                FROM account_groups g
                LEFT JOIN accounts a ON a.group_id = g.id
                GROUP BY g.id, g.name, g.sync_mode, g.sync_recent_days, g.article_count
                ORDER BY g.name ASC
                """
            )
            rows = cur.fetchall()
        return [
            AccountGroup(
                id=row['id'],
                name=row['name'],
                account_count=row['account_count'],
                article_count=row.get('article_count') or 0,
                sync_mode=row.get('sync_mode'),
                sync_recent_days=row.get('sync_recent_days'),
            )
            for row in rows
        ]


class LoginSessionRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def save_login_session(self, session: LoginSession, *, set_default: bool = True) -> LoginSession:
        now = utc_now_dt()
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
                except (json.JSONDecodeError, KeyError):
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
                        bool(set_default),
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
                        bool(set_default),
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
            cur.execute('SELECT updated_at FROM login_sessions WHERE is_default = TRUE ORDER BY id DESC LIMIT 1')
            row = cur.fetchone()
        if not row:
            return None
        updated_at = row.get('updated_at')
        if isinstance(updated_at, str):
            try:
                parsed = datetime.fromisoformat(updated_at)
            except ValueError:
                return None
            return to_utc_dt(parsed)
        if isinstance(updated_at, datetime):
            return to_utc_dt(updated_at)
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

    @staticmethod
    def _upsert_article_row(
        cur: psycopg.Cursor,
        *,
        biz: str,
        article_id: str,
        title: str,
        item_show_type: int | None,
        author: str | None,
        digest: str | None,
        link: str,
        source_url: str | None,
        publish_at: int | None,
        raw_json: str,
        now: datetime,
        return_inserted: bool = False,
    ) -> tuple[int, bool]:
        cur.execute(
            """
            INSERT INTO articles
                (biz, article_id, title, item_show_type, author, digest, cover, link, source_url,
                 publish_at, raw_json, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s)
            ON CONFLICT (biz, article_id) DO UPDATE SET
                title=EXCLUDED.title,
                item_show_type=COALESCE(EXCLUDED.item_show_type, articles.item_show_type),
                author=EXCLUDED.author,
                digest=EXCLUDED.digest,
                link=EXCLUDED.link,
                source_url=EXCLUDED.source_url,
                publish_at=EXCLUDED.publish_at,
                raw_json=EXCLUDED.raw_json,
                updated_at=EXCLUDED.updated_at
            RETURNING id"""
            + (', (xmax = 0) AS inserted' if return_inserted else ''),
            (
                biz,
                article_id,
                title,
                item_show_type,
                author,
                digest,
                None,
                link,
                source_url,
                publish_at,
                raw_json,
                now,
                now,
            ),
        )
        row = cur.fetchone()
        article_pk = int(row[0])
        is_inserted = bool(row[1]) if return_inserted else False
        return article_pk, is_inserted

    def save_articles(self, articles: Iterable[ArticleRecord]) -> int:
        now = utc_now_dt()
        inserted_count = 0
        with self._conn.cursor() as cur:
            for article in articles:
                article_pk, is_inserted = self._upsert_article_row(
                    cur,
                    biz=article.biz,
                    article_id=article.article_id,
                    title=article.title,
                    item_show_type=article.item_show_type,
                    author=article.author,
                    digest=article.digest,
                    link=article.link,
                    source_url=article.source_url,
                    publish_at=article.publish_at,
                    raw_json=json.dumps(article.raw, ensure_ascii=False),
                    now=now,
                    return_inserted=True,
                )
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
                        'UPDATE articles SET cover = %s, updated_at = %s WHERE id = %s',
                        (cover_id, now, article_pk),
                    )
                if is_inserted:
                    inserted_count += 1
        return inserted_count

    def save_article_content(
        self,
        article: ArticleRecord,
        *,
        url_token: str | None,
        title: str,
        item_show_type: int | None,
        clean_html: str,
        content_markdown: str,
        content_blocks: list[dict],
        cover_url: str | None,
        images: list[dict],
    ) -> None:
        now = utc_now_dt()
        normalized_cover: str | None = None
        with self._conn.cursor() as cur:
            if cover_url is not None:
                cover_id = self._normalize_cover_id(cover_url)
                if cover_id is not None:
                    cur.execute(
                        'SELECT orig_url FROM article_images WHERE id = %s',
                        (cover_id,),
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        normalized_cover = str(row[0]).strip()
                else:
                    normalized_cover = str(cover_url).strip()
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
            article_pk, _ = self._upsert_article_row(
                cur,
                biz=article.biz,
                article_id=article.article_id,
                title=title,
                item_show_type=item_show_type,
                author=article.author,
                digest=article.digest,
                link=article.link,
                source_url=article.source_url,
                publish_at=article.publish_at,
                raw_json=json.dumps(article.raw, ensure_ascii=False),
                now=now,
            )

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
                    'UPDATE articles SET cover = %s, updated_at = %s WHERE id = %s',
                    (cover_id, now, article_pk),
                )
            else:
                cur.execute(
                    'UPDATE articles SET cover = NULL, updated_at = %s WHERE id = %s',
                    (now, article_pk),
                )

    def has_article_content(self, biz: str, article_id: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT 1
                FROM article_content c
                JOIN articles a ON a.id = c.article_pk
                WHERE a.biz = %s AND a.article_id = %s
                  AND {ARTICLE_CONTENT_PRESENT_SQL}
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
                f"""
                SELECT a.article_id
                FROM article_content c
                JOIN articles a ON a.id = c.article_pk
                WHERE a.biz = %s AND a.article_id = ANY(%s)
                  AND {ARTICLE_CONTENT_PRESENT_SQL}
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
            query_parts.append(f'AND (c.id IS NULL OR NOT {ARTICLE_CONTENT_PRESENT_SQL})')

        if since_timestamp is not None:
            query_parts.append('AND (a.publish_at IS NULL OR a.publish_at >= %s)')
            params.append(since_timestamp)

        query_parts.append('ORDER BY a.publish_at DESC NULLS LAST, a.id DESC')

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

    def get_image_hash(self, image_id: int) -> dict[str, Any] | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, article_pk, hash_algo, content_hash
                FROM article_images
                WHERE id = %s
                """,
                (image_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def save_image_hash(
        self,
        *,
        image_id: int,
        hash_algo: str,
        content_hash: str,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE article_images
                SET hash_algo = %s,
                    content_hash = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (hash_algo, content_hash, utc_now_dt(), image_id),
            )
            if cur.rowcount == 0:
                raise LookupError(f'Image {image_id} not found')

    def block_image_hash(
        self,
        *,
        hash_algo: str,
        content_hash: str,
        source_image_id: int | None,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO blocked_image_hashes (hash_algo, content_hash, source_image_id, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (hash_algo, content_hash) DO UPDATE SET
                    source_image_id = COALESCE(blocked_image_hashes.source_image_id, EXCLUDED.source_image_id)
                """,
                (hash_algo, content_hash, source_image_id, utc_now_dt()),
            )

    def has_blocked_hashes(self) -> bool:
        with self._conn.cursor() as cur:
            cur.execute('SELECT 1 FROM blocked_image_hashes LIMIT 1')
            return cur.fetchone() is not None

    def get_article_images(self, article_pk: int) -> list[dict[str, Any]]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, position, kind, content_type, hash_algo, content_hash
                FROM article_images
                WHERE article_pk = %s
                ORDER BY position ASC
                """,
                (article_pk,),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def list_blocked_image_ids(self, article_pk: int) -> set[int]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.id
                FROM article_images i
                JOIN blocked_image_hashes b
                  ON b.hash_algo = i.hash_algo
                 AND b.content_hash = i.content_hash
                WHERE i.article_pk = %s
                """,
                (article_pk,),
            )
            rows = cur.fetchall()
        return {int(row[0]) for row in rows}

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
                    utc_now_dt(),
                    article_pk,
                    orig_url,
                ),
            )

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
                    utc_now_dt(),
                    trimmed,
                    utc_now_dt(),
                    article_pk,
                    orig_url,
                ),
            )

    def list_s3_keys_for_account(self, biz: str) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.s3_key
                FROM article_images i
                JOIN articles a ON a.id = i.article_pk
                WHERE a.biz = %s AND i.s3_key IS NOT NULL AND i.s3_key <> ''
                """,
                (biz,),
            )
            return [row[0] for row in cur.fetchall()]


def _row_to_account(row: dict[str, Any]) -> AccountCredential:
    return AccountCredential.model_validate(row)


def _row_to_article(row: dict[str, Any]) -> ArticleRecord:
    data = dict(row)
    raw_json = data.pop('raw_json', None)
    data['raw'] = json.loads(raw_json) if raw_json else {}
    return ArticleRecord.model_validate(data)


__all__ = [
    'AccountRepository',
    'ArticleImageTarget',
    'ArticleRepository',
    'GroupRepository',
    'ImageRepository',
    'LoginSessionRepository',
    'MetaRepository',
]
