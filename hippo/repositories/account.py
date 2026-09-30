"""Accounts and the per-user groups they belong to."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from ..models import AccountCredential, AccountGroup
from ..utils import build_set_clause, utc_now_dt


def _row_to_account(row: dict[str, Any]) -> AccountCredential:
    return AccountCredential.model_validate(row)


class AccountRepository:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def upsert_account(self, account: AccountCredential) -> AccountCredential:
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO accounts (biz, nickname, alias, gh_id, round_head_img,
                                      group_id, is_disabled, sync_mode, sync_recent_days,
                                      sync_interval_days, last_synced_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (biz) DO UPDATE SET
                    nickname=EXCLUDED.nickname,
                    alias=EXCLUDED.alias,
                    gh_id=COALESCE(EXCLUDED.gh_id, accounts.gh_id),
                    round_head_img=EXCLUDED.round_head_img,
                    updated_at=EXCLUDED.updated_at
                """,
                (
                    account.biz,
                    account.nickname,
                    account.alias,
                    account.gh_id,
                    account.round_head_img,
                    account.group_id,
                    account.is_disabled,
                    account.sync_mode,
                    account.sync_recent_days,
                    account.sync_interval_days,
                    account.last_synced_at,
                    now,
                    now,
                ),
            )
        return await self.get_account(account.biz, fallback_to_default=False)

    async def list_accounts(self, *, group: str | None = None) -> list[AccountCredential]:
        query = 'SELECT a.*, g.name AS group_name FROM accounts a LEFT JOIN account_groups g ON g.id = a.group_id'
        params: list = []
        if group:
            query += ' WHERE g.name = %s'
            params.append(group)
        query += ' ORDER BY a.nickname ASC'
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()
        return [_row_to_account(row) for row in rows]

    async def list_followed_accounts(
        self,
        user_id: int,
        *,
        group: str | None = None,
    ) -> list[AccountCredential]:
        """Accounts the user follows, carrying their per-user flags.

        ``a.*`` is expanded first so the subscription columns that share a name
        (group_id, is_disabled, sync_interval_days) overwrite the legacy values
        still sitting on the shared account row.
        """
        query = (
            'SELECT a.*, s.group_id, s.is_disabled, s.sync_interval_days, g.name AS group_name'
            ' FROM accounts a'
            ' JOIN subscription s ON s.biz = a.biz AND s.user_id = %s'
            ' LEFT JOIN account_groups g ON g.id = s.group_id'
        )
        params: list[Any] = [user_id]
        if group:
            query += ' WHERE g.name = %s'
            params.append(group)
        query += ' ORDER BY a.nickname ASC'
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()
        return [_row_to_account(row) for row in rows]

    async def list_syncable_accounts(
        self,
        *,
        group_ids: list[int] | None = None,
        biz_list: list[str] | None = None,
    ) -> list[AccountCredential]:
        """Accounts that at least one user still follows.

        The catalogue is shared across users, so crawling is driven by
        subscriptions: an account is fetched while somebody follows it, and the
        data already collected stays once the last subscriber leaves.

        ``sync_interval_days`` is aggregated with ``MIN`` so the most eager
        subscriber decides how often the shared account gets refreshed, and
        ``group_ids`` matches any subscriber's grouping.
        """
        query = """
            SELECT a.*, g.name AS group_name, agg.sync_interval_days
            FROM accounts a
            JOIN (
                SELECT s.biz,
                       MIN(s.sync_interval_days) AS sync_interval_days,
                       array_agg(DISTINCT s.group_id) AS group_ids
                FROM subscription s
                WHERE NOT s.is_disabled
                GROUP BY s.biz
            ) agg ON agg.biz = a.biz
            LEFT JOIN account_groups g ON g.id = a.group_id
            WHERE a.is_disabled = FALSE
        """
        params: list[Any] = []
        if group_ids:
            query += ' AND agg.group_ids && %s::int[]'
            params.append(list(group_ids))
        if biz_list:
            query += ' AND a.biz = ANY(%s)'
            params.append(list(biz_list))
        query += ' ORDER BY a.nickname ASC'
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()
        return [_row_to_account(row) for row in rows]

    async def set_gh_id(self, biz: str, gh_id: str) -> int:
        """缓存公众号的 gh_（列表接口只认它；解析一次就够了）。"""
        async with self._conn.cursor() as cur:
            await cur.execute(
                'UPDATE accounts SET gh_id = %s, updated_at = NOW() WHERE biz = %s',
                (gh_id, biz),
            )
            return cur.rowcount

    async def get_account(self, biz: str | None = None, *, fallback_to_default: bool = True) -> AccountCredential:
        row = None
        async with self._conn.cursor(row_factory=dict_row) as cur:
            if biz:
                await cur.execute(
                    """
                    SELECT a.*, g.name AS group_name
                    FROM accounts a
                    LEFT JOIN account_groups g ON g.id = a.group_id
                    WHERE a.biz = %s
                    """,
                    (biz,),
                )
                row = await cur.fetchone()
            if not row and fallback_to_default and not biz:
                await cur.execute(
                    """
                    SELECT a.*, g.name AS group_name
                    FROM accounts a
                    LEFT JOIN account_groups g ON g.id = a.group_id
                    ORDER BY a.updated_at DESC
                    LIMIT 1
                    """
                )
                row = await cur.fetchone()
        if not row:
            raise LookupError('No account found. Create one with `accounts add` or `accounts search --interactive`.')
        return _row_to_account(row)

    async def remove_account(self, biz: str) -> int:
        async with self._conn.cursor() as cur:
            await cur.execute('DELETE FROM accounts WHERE biz = %s', (biz,))
            removed = cur.rowcount
        return removed


    async def update_last_synced(self, biz: str) -> None:
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            await cur.execute(
                'UPDATE accounts SET last_synced_at = %s, updated_at = %s WHERE biz = %s',
                (now, now, biz),
            )


    async def list_accounts_paginated(
        self,
        *,
        user_id: int,
        group_ids: list[int] | None = None,
        search_tokens: list[str] | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        where: list[str] = []
        params: list[Any] = []
        if group_ids is not None:
            where.append('s.group_id = ANY(%s)')
            params.append(group_ids)
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
        # Grouping and the disabled flag now come from the caller's subscription;
        # the account row only contributes the shared catalogue fields.
        query_sql = (
            'WITH filtered_accounts AS ('
            ' SELECT a.biz, a.nickname, a.alias, a.round_head_img, s.group_id,'
            ' s.is_disabled, a.last_synced_at, a.sync_mode, a.sync_recent_days,'
            ' s.sync_interval_days, a.article_count'
            ' FROM accounts a'
            ' JOIN subscription s ON s.biz = a.biz AND s.user_id = %s'
            f' {where_sql}'
            ' ORDER BY a.nickname ASC'
            ' LIMIT %s OFFSET %s'
            ')'
            ' SELECT a.biz, a.nickname, a.alias, a.round_head_img, a.group_id,'
            ' a.is_disabled, a.last_synced_at, a.sync_mode, a.sync_recent_days, g.name AS group_name,'
            ' a.sync_interval_days,'
            ' COALESCE(a.article_count, 0) AS article_count,'
            ' (ai.data IS NOT NULL) AS avatar_ready'
            ' FROM filtered_accounts a'
            ' LEFT JOIN account_groups g ON g.id = a.group_id'
            ' LEFT JOIN avatar_images ai ON ai.biz = a.biz'
            ' ORDER BY a.nickname ASC'
        )
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query_sql, [user_id, *params, page_size, offset])
            rows = [dict(row) for row in await cur.fetchall()]
        for row in rows:
            row['avatar_url'] = f'/api/account/{row["biz"]}/avatar'
        count_sql = (
            'SELECT COUNT(*) AS total FROM accounts a'
            ' JOIN subscription s ON s.biz = a.biz AND s.user_id = %s'
            f' {where_sql}'
        )
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(count_sql, [user_id, *params])
            total_row = await cur.fetchone()
        total = int(total_row['total']) if total_row else 0
        return {'accounts': rows, 'page': page, 'page_size': page_size, 'total': total}

    async def get_account_detail(self, biz: str, *, user_id: int) -> dict[str, Any]:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                'SELECT a.biz, a.nickname, a.alias, a.round_head_img, s.group_id,'
                ' s.is_disabled, a.last_synced_at, a.sync_mode, a.sync_recent_days,'
                ' s.sync_interval_days, g.name AS group_name,'
                ' COALESCE(a.article_count, 0) AS article_count,'
                ' (ai.data IS NOT NULL) AS avatar_ready'
                ' FROM accounts a'
                ' JOIN subscription s ON s.biz = a.biz AND s.user_id = %s'
                ' LEFT JOIN account_groups g ON g.id = s.group_id'
                ' LEFT JOIN avatar_images ai ON ai.biz = a.biz'
                ' WHERE a.biz = %s',
                (user_id, biz),
            )
            row = await cur.fetchone()
        if not row:
            raise LookupError('Account not found')
        result = dict(row)
        result['avatar_url'] = f'/api/account/{result["biz"]}/avatar'
        return result

    async def update_account_fields(self, biz: str, **updates: Any) -> None:
        """Update the shared catalogue fields.

        Per-user fields (group, disabled flag, sync interval) live on
        ``subscription`` and are written through ``SubscriptionRepository``.
        """
        fields, params = build_set_clause(
            {
                'nickname': 'nickname',
                'alias': 'alias',
                'round_head_img': 'round_head_img',
            },
            updates,
        )
        if not fields:
            raise ValueError('No fields to update')
        fields.append('updated_at = NOW()')
        params.append(biz)
        async with self._conn.cursor() as cur:
            await cur.execute(
                f'UPDATE accounts SET {", ".join(fields)} WHERE biz = %s',
                params,
            )
            if cur.rowcount == 0:
                raise LookupError('Account not found')

    async def get_latest_publish_at(self, biz: str) -> datetime | None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                'SELECT publish_at FROM articles WHERE biz = %s AND publish_at IS NOT NULL'
                ' ORDER BY publish_at DESC LIMIT 1',
                (biz,),
            )
            row = await cur.fetchone()
        if not row:
            return None
        ts = row[0]
        if ts is None:
            return None
        return datetime.fromtimestamp(ts, tz=UTC)



class GroupRepository:
    """Groups belong to a user even though the account catalogue is shared.

    ``AccountRepository`` stays global; everything that expresses "how *I*
    organise and follow accounts" lives here or in the subscription table.
    """

    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def get_group(self, group_id: int, *, user_id: int) -> AccountGroup:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    g.id,
                    g.name,
                    g.sync_mode,
                    g.sync_recent_days,
                    COALESCE(g.article_count, 0) AS article_count,
                    COUNT(s.biz) AS account_count
                FROM account_groups g
                LEFT JOIN subscription s
                       ON s.group_id = g.id AND s.user_id = %s AND NOT s.is_disabled
                WHERE g.id = %s AND g.user_id = %s
                GROUP BY g.id, g.name, g.sync_mode, g.sync_recent_days, g.article_count
                """,
                (user_id, group_id, user_id),
            )
            row = await cur.fetchone()
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

    async def update_group(self, group_id: int, *, user_id: int, **updates: Any) -> AccountGroup:
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
        params.extend([group_id, user_id])
        async with self._conn.cursor() as cur:
            await cur.execute(
                f'UPDATE account_groups SET {", ".join(fields)} WHERE id = %s AND user_id = %s',
                params,
            )
            if cur.rowcount == 0:
                raise LookupError('Group not found')
        return await self.get_group(group_id, user_id=user_id)

    async def delete_group(self, group_id: int, default_group_id: int, *, user_id: int) -> None:
        if group_id == default_group_id:
            raise ValueError('Default group cannot be deleted')
        async with self._conn.cursor() as cur:
            # Only this user's subscriptions move; other users keep their grouping.
            await cur.execute(
                'UPDATE subscription SET group_id = %s WHERE user_id = %s AND group_id = %s',
                (default_group_id, user_id, group_id),
            )
            await cur.execute(
                'DELETE FROM account_groups WHERE id = %s AND user_id = %s',
                (group_id, user_id),
            )
            if cur.rowcount == 0:
                raise LookupError('Group not found')

    async def upsert_group(self, name: str, *, user_id: int) -> AccountGroup:
        trimmed = name.strip()
        if not trimmed:
            raise ValueError('Group name cannot be empty.')
        now = utc_now_dt()
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO account_groups (name, user_id, created_at, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, name) DO UPDATE SET updated_at = EXCLUDED.updated_at
                RETURNING id, name
                """,
                (trimmed, user_id, now, now),
            )
            row = await cur.fetchone()
        if not row:
            raise RuntimeError(f'Failed to create group {trimmed}.')
        return AccountGroup(id=row['id'], name=row['name'])

    async def list_groups(self, *, user_id: int) -> list[AccountGroup]:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    g.id,
                    g.name,
                    g.sync_mode,
                    g.sync_recent_days,
                    COUNT(s.biz) AS account_count,
                    COALESCE(g.article_count, 0) AS article_count
                FROM account_groups g
                LEFT JOIN subscription s
                       ON s.group_id = g.id AND s.user_id = %s AND NOT s.is_disabled
                WHERE g.user_id = %s
                GROUP BY g.id, g.name, g.sync_mode, g.sync_recent_days, g.article_count
                ORDER BY g.name ASC
                """,
                (user_id, user_id),
            )
            rows = await cur.fetchall()
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
