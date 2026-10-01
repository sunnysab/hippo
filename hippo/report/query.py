"""Daily report queries.

A "day" is the user's local day, not the server's: the same instant falls on
different dates for readers in different time zones, and the report is built for
the reader. ``publish_at`` is stored as a Unix timestamp, so the window is
computed in the user's zone and converted back to epoch seconds for the query.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg.rows import dict_row

#: Fallback when a user's stored time zone is unknown to the runtime database.
DEFAULT_TIMEZONE = 'Asia/Shanghai'


def resolve_timezone(name: str | None) -> ZoneInfo:
    """Return the zone, falling back rather than failing a scheduled run."""
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo(DEFAULT_TIMEZONE)


def day_bounds(report_date: date, timezone_name: str | None) -> tuple[int, int]:
    """Return the half-open epoch window ``[start, end)`` of a local day."""
    tz = resolve_timezone(timezone_name)
    start = datetime.combine(report_date, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def local_date(now: datetime, timezone_name: str | None) -> date:
    """The date ``now`` falls on for this user."""
    tz = resolve_timezone(timezone_name)
    return now.astimezone(tz).date() if now.tzinfo else now.replace(tzinfo=UTC).astimezone(tz).date()


def parse_date(raw: str | None, timezone_name: str | None) -> date:
    """Parse a ``YYYY-MM-DD`` query parameter, defaulting to the user's today."""
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return local_date(datetime.now(UTC), timezone_name)


class ReportRepository:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def get_setting(self, user_id: int) -> dict[str, Any]:
        """The user's report preferences, with defaults filled in."""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT enabled, send_hour, recipients, group_ids, include_read
                FROM report_setting WHERE user_id = %s
                """,
                (user_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return {
                'enabled': False,
                'send_hour': 8,
                'recipients': [],
                'group_ids': [],
                'include_read': True,
            }
        return {
            'enabled': bool(row['enabled']),
            'send_hour': int(row['send_hour']),
            'recipients': list(row['recipients'] or []),
            'group_ids': [int(item) for item in (row['group_ids'] or [])],
            'include_read': bool(row['include_read']),
        }

    async def save_setting(self, user_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        current = await self.get_setting(user_id)
        for key in ('enabled', 'send_hour', 'recipients', 'group_ids', 'include_read'):
            if key in updates:
                current[key] = updates[key]
        current['send_hour'] = min(max(int(current['send_hour']), 0), 23)
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO report_setting
                    (user_id, enabled, send_hour, recipients, group_ids, include_read, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    send_hour = EXCLUDED.send_hour,
                    recipients = EXCLUDED.recipients,
                    group_ids = EXCLUDED.group_ids,
                    include_read = EXCLUDED.include_read,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    user_id,
                    bool(current['enabled']),
                    int(current['send_hour']),
                    list(current['recipients']),
                    [int(item) for item in current['group_ids']],
                    bool(current['include_read']),
                    datetime.now(UTC),
                ),
            )
        return current

    async def users_due(self, hour: int) -> list[dict[str, Any]]:
        """Enabled reports whose send hour has arrived in the user's own zone."""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT r.user_id, r.send_hour, r.recipients, r.group_ids, r.include_read,
                       u.username, u.timezone
                FROM report_setting r
                JOIN users u ON u.id = r.user_id
                WHERE r.enabled AND NOT u.is_disabled AND r.send_hour = %s
                """,
                (hour,),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def articles_for_day(
        self,
        user_id: int,
        *,
        start: int,
        end: int,
        group_ids: list[int] | None = None,
        include_read: bool = True,
    ) -> list[dict[str, Any]]:
        """Articles published in the window that this user subscribes to.

        Group membership lives on ``subscription.group_id``, so the group filter
        is a predicate on the same row as the subscription itself.
        """
        clauses = ['a.publish_at >= %s', 'a.publish_at < %s']
        params: list[Any] = [user_id, start, end]
        if group_ids:
            clauses.append('s.group_id = ANY(%s)')
            params.append(group_ids)
        if not include_read:
            # Only articles with no read row for this user.
            clauses.append('ar.article_pk IS NULL')

        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT a.id, a.title, a.digest, a.link, a.author, a.publish_at,
                       a.cover, a.biz, ac.nickname,
                       s.group_id, g.name AS group_name
                FROM articles a
                JOIN subscription s ON s.biz = a.biz AND s.user_id = %s
                LEFT JOIN accounts ac ON ac.biz = a.biz
                LEFT JOIN account_groups g ON g.id = s.group_id
                LEFT JOIN article_read ar ON ar.article_pk = a.id AND ar.user_id = s.user_id
                WHERE {' AND '.join(clauses)}
                ORDER BY a.publish_at DESC, a.id DESC
                """,
                params,
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def record_delivery(
        self,
        user_id: int,
        report_date: date,
        channel: str,
        status: str,
        error: str | None = None,
    ) -> bool:
        """Record a delivery attempt. Returns False when one already exists.

        The unique constraint is the idempotency guarantee: a restart or a
        duplicate scheduler tick hits ``ON CONFLICT DO NOTHING`` and the caller
        learns it must not send again.
        """
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO report_delivery
                    (user_id, report_date, channel, status, error, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, report_date, channel) DO NOTHING
                """,
                (user_id, report_date, channel, status, error, datetime.now(UTC)),
            )
            return cur.rowcount > 0

    async def delivery_exists(self, user_id: int, report_date: date, channel: str) -> bool:
        async with self._conn.cursor() as cur:
            await cur.execute(
                'SELECT 1 FROM report_delivery WHERE user_id = %s AND report_date = %s AND channel = %s',
                (user_id, report_date, channel),
            )
            return await cur.fetchone() is not None
