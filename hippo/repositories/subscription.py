"""Per-user subscriptions.

Which accounts a user follows, in which group, with which sync policy. The
catalogue itself (``accounts``, ``articles``) stays global because the WeChat
daemon only ever has one login.

Every method here takes an explicit ``user_id``: this is the table that makes a
query user-scoped, so there is no default to fall back on.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from ..utils import utc_now_dt

#: Columns returned alongside every account row so the API can keep its shape.
SUBSCRIPTION_FIELDS = 's.group_id, s.is_disabled, s.sync_interval_days'


class SubscriptionRepository:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def upsert(
        self,
        user_id: int,
        biz: str,
        *,
        group_id: int | None = None,
        is_disabled: bool | None = None,
        sync_interval_days: int | None = None,
    ) -> None:
        """Follow an account, or update the fields that were provided."""
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO subscription
                    (user_id, biz, group_id, is_disabled, sync_interval_days, created_at)
                VALUES (%s, %s, %s, COALESCE(%s, FALSE), %s, %s)
                ON CONFLICT (user_id, biz) DO UPDATE SET
                    group_id = COALESCE(EXCLUDED.group_id, subscription.group_id),
                    is_disabled = COALESCE(%s, subscription.is_disabled),
                    sync_interval_days = CASE
                        WHEN %s THEN EXCLUDED.sync_interval_days
                        ELSE subscription.sync_interval_days
                    END
                """,
                (
                    user_id,
                    biz,
                    group_id,
                    is_disabled,
                    sync_interval_days,
                    utc_now_dt(),
                    is_disabled,
                    sync_interval_days is not None,
                ),
            )

    async def add_many(self, user_id: int, biz_list: list[str], *, group_id: int | None = None) -> int:
        """Follow several accounts at once, ignoring the ones already followed."""
        if not biz_list:
            return 0
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO subscription (user_id, biz, group_id, is_disabled, created_at)
                SELECT %s, biz, %s, FALSE, %s FROM unnest(%s::text[]) AS biz
                ON CONFLICT (user_id, biz) DO NOTHING
                """,
                (user_id, group_id, now, list(biz_list)),
            )
            return cur.rowcount or 0

    async def remove_many(self, user_id: int, biz_list: list[str]) -> int:
        if not biz_list:
            return 0
        async with self._conn.cursor() as cur:
            await cur.execute(
                'DELETE FROM subscription WHERE user_id = %s AND biz = ANY(%s)',
                (user_id, list(biz_list)),
            )
            return cur.rowcount or 0

    async def set_disabled(self, user_id: int, biz_list: list[str], is_disabled: bool) -> int:
        if not biz_list:
            return 0
        async with self._conn.cursor() as cur:
            await cur.execute(
                'UPDATE subscription SET is_disabled = %s WHERE user_id = %s AND biz = ANY(%s)',
                (is_disabled, user_id, list(biz_list)),
            )
            return cur.rowcount or 0

    async def set_interval(self, user_id: int, biz_list: list[str], days: int | None) -> int:
        if not biz_list:
            return 0
        async with self._conn.cursor() as cur:
            await cur.execute(
                'UPDATE subscription SET sync_interval_days = %s WHERE user_id = %s AND biz = ANY(%s)',
                (days, user_id, list(biz_list)),
            )
            return cur.rowcount or 0

    async def set_group(self, user_id: int, biz_list: list[str], group_id: int | None) -> int:
        if not biz_list:
            return 0
        async with self._conn.cursor() as cur:
            await cur.execute(
                'UPDATE subscription SET group_id = %s WHERE user_id = %s AND biz = ANY(%s)',
                (group_id, user_id, list(biz_list)),
            )
            return cur.rowcount or 0

    async def list_for_user(self, user_id: int) -> list[dict[str, Any]]:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT s.biz, s.group_id, s.is_disabled, s.sync_interval_days, s.created_at
                FROM subscription s WHERE s.user_id = %s
                ORDER BY s.biz
                """,
                (user_id,),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def subscribers_of(self, biz: str) -> int:
        """How many users still follow an account."""
        async with self._conn.cursor() as cur:
            await cur.execute(
                'SELECT count(*) FROM subscription WHERE biz = %s AND NOT is_disabled',
                (biz,),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def claim_orphan_groups(self, user_id: int) -> int:
        """Assign groups created before multi-user support to ``user_id``."""
        async with self._conn.cursor() as cur:
            await cur.execute(
                'UPDATE account_groups SET user_id = %s WHERE user_id IS NULL',
                (user_id,),
            )
            return cur.rowcount or 0

    async def backfill_from_accounts(self, user_id: int) -> int:
        """Copy the legacy per-account settings into ``subscription`` rows."""
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO subscription
                    (user_id, biz, group_id, is_disabled, sync_interval_days, created_at)
                SELECT %s, biz, group_id, is_disabled, sync_interval_days, %s
                FROM accounts
                ON CONFLICT (user_id, biz) DO NOTHING
                """,
                (user_id, utc_now_dt()),
            )
            return cur.rowcount or 0
