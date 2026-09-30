"""Daily report endpoints: settings, JSON preview and rendered HTML."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, status
from fastapi.responses import HTMLResponse

from ...exceptions import ApiError
from ...models import User
from ...report import ReportRepository, build_context, day_bounds, parse_date, render_report
from ...site_settings import get_site_settings
from ...storage import PostgresStorage
from ..deps import current_user, get_storage

router = APIRouter(dependencies=[Depends(current_user)])


def _normalize_group_ids(raw: Any) -> list[int]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ApiError('group_ids 必须是数组', status=400)
    result: list[int] = []
    for item in raw:
        try:
            result.append(int(item))
        except (TypeError, ValueError) as exc:
            raise ApiError('group_ids 含非整数', status=400) from exc
    return result


def _normalize_recipients(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ApiError('recipients 必须是数组', status=400)
    return [str(item).strip() for item in raw if str(item).strip()]


async def _collect(
    storage: PostgresStorage,
    user: User,
    date_param: str | None,
    group_ids: list[int] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Resolve the user's window, settings and articles for one local day."""
    settings = await storage.reports.get_setting(user.id)
    report_date = parse_date(date_param, user.timezone)
    start, end = day_bounds(report_date, user.timezone)
    effective_groups = group_ids if group_ids is not None else settings['group_ids']
    articles = await storage.reports.articles_for_day(
        user.id,
        start=start,
        end=end,
        group_ids=effective_groups,
        include_read=settings['include_read'],
    )
    context = build_context(
        username=user.username,
        report_date=report_date,
        timezone_name=user.timezone,
        articles=articles,
        site_name=(await get_site_settings(storage))['site_name'],
        public_base_url=(await get_site_settings(storage))['public_base_url'],
    )
    return settings, articles, context


@router.get('/report/setting')
async def read_setting(
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """This user's report preferences."""
    return await storage.reports.get_setting(user.id)


@router.patch('/report/setting')
async def update_setting(
    body: dict[str, Any] = Body(default={}),
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Update report preferences: switch, send hour, recipients and scope."""
    updates: dict[str, Any] = {}
    if 'enabled' in body:
        updates['enabled'] = bool(body['enabled'])
    if 'send_hour' in body:
        try:
            updates['send_hour'] = int(body['send_hour'])
        except (TypeError, ValueError) as exc:
            raise ApiError('send_hour 必须是整数', status=400) from exc
        if not 0 <= updates['send_hour'] <= 23:
            raise ApiError('send_hour 必须在 0-23 之间', status=400)
    if 'recipients' in body:
        updates['recipients'] = _normalize_recipients(body['recipients'])
    if 'group_ids' in body:
        updates['group_ids'] = _normalize_group_ids(body['group_ids'])
    if 'include_read' in body:
        updates['include_read'] = bool(body['include_read'])
    if not updates:
        raise ApiError('没有需要更新的字段', status=400)

    async with storage.transaction():
        return await storage.reports.save_setting(user.id, updates)


@router.get('/report/{date}')
async def read_report(
    date: str,
    group_ids: str | None = Query(default=None, description='逗号分隔的分组 ID'),
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """JSON view of one local day's report, for the preview page."""
    parsed_groups = [int(item) for item in group_ids.split(',') if item.strip()] if group_ids else None
    settings, _articles, context = await _collect(storage, user, date, parsed_groups)
    return {
        'date': context['report_date'],
        'timezone': user.timezone,
        'total': context['total'],
        'groups': context['groups'],
        'setting': settings,
    }


@router.get('/report/{date}/html', response_class=HTMLResponse)
async def read_report_html(
    date: str,
    group_ids: str | None = Query(default=None),
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> HTMLResponse:
    """The exact HTML that would be e-mailed, for the preview iframe."""
    parsed_groups = [int(item) for item in group_ids.split(',') if item.strip()] if group_ids else None
    _, _, context = await _collect(storage, user, date, parsed_groups)
    return HTMLResponse(render_report(context))


@router.post('/report/{date}/send', status_code=status.HTTP_202_ACCEPTED)
async def send_report_now(
    date: str,
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Send today's report immediately, bypassing the schedule.

    Still goes through the delivery ledger, so pressing the button twice on the
    same day sends one e-mail, not two.
    """
    from ...report.delivery import deliver_once

    settings, _articles, context = await _collect(storage, user, date, None)
    recipients = settings['recipients'] or ([user.email] if user.email else [])
    if not recipients:
        raise ApiError('未配置收件人，且账号没有邮箱', status=400)

    result = await deliver_once(
        storage,
        user_id=user.id,
        report_date=parse_date(date, user.timezone),
        recipients=recipients,
        subject=f'{context["site_name"]} 日报 · {context["report_date"]}',
        html=render_report(context),
        text=f'{context["report_date"]} 日报，共 {context["total"]} 篇。',
    )
    return result


__all__ = ['ReportRepository', 'router']
