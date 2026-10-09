"""Report delivery.

``report_delivery`` is the idempotency ledger: the unique constraint on
``(user_id, report_date, channel)`` means a restart, an overlapping scheduler
tick or an impatient second click can only ever produce one send.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from ..logger import get_logger
from ..storage import PostgresStorage

logger = get_logger(__name__)

#: Delivery channel recorded in the ledger. Only e-mail exists today, but the
#: column exists so a second channel does not have to reinterpret old rows.
CHANNEL_EMAIL = 'email'


async def deliver_once(
    storage: PostgresStorage,
    *,
    user_id: int,
    report_date: date,
    recipients: list[str],
    subject: str,
    html: str,
    text: str,
) -> dict[str, Any]:
    """Send the report unless it was already delivered today.

    The ledger row is claimed *and committed* before the send: claiming after would
    let two concurrent callers both send, and a failed claim is cheaper to explain
    than a duplicate e-mail.
    """
    async with storage.transaction():
        claimed = await storage.reports.record_delivery(user_id, report_date, CHANNEL_EMAIL, 'pending')
    if not claimed:
        logger.debug('Report already delivered: user=%s date=%s', user_id, report_date.isoformat())
        return {'sent': False, 'reason': 'already_delivered'}

    # Imported here, not at module level: emailer imports storage, which
    # imports this package.
    from ..emailer import get_email_settings

    email_settings = await get_email_settings(storage)
    if not str(email_settings.get('smtp_host') or '').strip():
        await _mark(storage, user_id, report_date, 'skipped', '未配置 SMTP')
        return {'sent': False, 'reason': 'smtp_not_configured'}

    try:
        from ..emailer import send_email

        for recipient in recipients:
            await asyncio.to_thread(
                send_email,
                email_settings,
                to_email=recipient,
                subject=subject,
                body=text,
                html=html,
            )
    except Exception as exc:
        logger.warning('Report delivery failed: user=%s date=%s: %s', user_id, report_date, exc)
        await _mark(storage, user_id, report_date, 'failed', str(exc))
        return {'sent': False, 'reason': 'send_failed', 'error': str(exc)}

    await _mark(storage, user_id, report_date, 'sent', None)
    return {'sent': True, 'recipients': recipients}


async def _mark(
    storage: PostgresStorage,
    user_id: int,
    report_date: date,
    status: str,
    error: str | None,
) -> None:
    """Update the claimed row's outcome.

    Goes through the caller's connection: the claim was committed before the
    send, so this is a plain UPDATE and no second connection is involved.
    """
    try:
        async with storage.transaction():
            await storage.reports.mark_delivery(user_id, report_date, CHANNEL_EMAIL, status, error)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning('Failed to record report delivery status: %s', exc)


async def deliver_due(storage: PostgresStorage, hour: int) -> int:
    """Send every enabled report whose local send hour is ``hour``.

    Returns how many were actually sent. Each user's own time zone decides
    whether the current hour matches, which is why the query filters on
    ``send_hour`` and this function re-checks the local hour.
    """
    from ..report import day_bounds, local_date, render_html
    from ..site_settings import get_site_settings
    from ..utils import utc_now_dt

    due = await storage.reports.users_due(hour)
    if not due:
        return 0

    site = await get_site_settings(storage)
    now = utc_now_dt()
    sent = 0
    for row in due:
        # A user whose zone puts the current hour elsewhere is skipped here:
        # the scheduler ticks hourly, so it will come around again.
        if not _hour_matches(now, row['timezone'], hour):
            continue
        report_date = local_date(now, row['timezone'])
        start, end = day_bounds(report_date, row['timezone'])
        articles = await storage.reports.articles_for_day(
            row['user_id'],
            start=start,
            end=end,
            group_ids=[int(item) for item in (row['group_ids'] or [])],
            include_read=bool(row['include_read']),
        )
        html = render_html(
            username=row['username'],
            report_date=report_date,
            timezone_name=row['timezone'],
            articles=articles,
            site_name=site['site_name'],
            public_base_url=site['public_base_url'],
        )
        recipients = list(row['recipients'] or [])
        if not recipients:
            await _mark(storage, row['user_id'], report_date, 'skipped', '未配置收件人')
            continue
        result = await deliver_once(
            storage,
            user_id=row['user_id'],
            report_date=report_date,
            recipients=recipients,
            subject=f'{site["site_name"]} 日报 · {report_date.isoformat()}',
            html=html,
            text=f'{report_date.isoformat()} 日报，共 {len(articles)} 篇。',
        )
        if result.get('sent'):
            sent += 1
    return sent


def _hour_matches(now: Any, timezone_name: str | None, hour: int) -> bool:
    from .query import resolve_timezone

    return now.astimezone(resolve_timezone(timezone_name)).hour == hour


__all__ = ['CHANNEL_EMAIL', 'deliver_due', 'deliver_once']
