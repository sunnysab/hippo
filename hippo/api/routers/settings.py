"""Sync, filter and mail settings endpoints."""

import asyncio
import time as time_module
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, status

from ...article_queries import _normalize_record
from ...emailer import get_email_settings, send_email, set_email_settings
from ...exceptions import ApiError
from ...logger import get_logger
from ...models import User
from ...storage import PostgresStorage, fetchone_row, load_meta_json
from ...sync_core import request_sync_cancel
from ...sync_scheduler import SyncScheduler
from ...sync_settings import QUEUE_STATS_KEY, WORKER_HEARTBEAT_KEY
from ...sync_settings import get_sync_settings as load_sync_settings
from ...sync_settings import get_sync_status as load_sync_status
from ...sync_settings import set_sync_settings as save_sync_settings
from ..deps import current_user, get_storage, get_sync_scheduler, require_admin

logger = get_logger(__name__)
router = APIRouter(dependencies=[Depends(current_user)])


@router.get('/settings/status')
async def sync_status(
    limit: int = 5,
    storage: PostgresStorage = Depends(get_storage),
    _: User = Depends(current_user),
) -> dict[str, Any]:
    """
    获取后台同步任务的状态。

    Returns:
        dict: 同步状态详情。
    """
    payload = await load_sync_status(storage)
    history = payload.get('history')
    if isinstance(history, list):
        normalized_limit = min(max(int(limit), 1), 50)
        payload['history'] = history[:normalized_limit]
    # 队列水位由 worker 定期写进 meta（web 轮询不扫 article_images 那种大表）
    queue = await load_meta_json(storage, QUEUE_STATS_KEY, {}) or {}
    queue['failed_items'] = await storage.article_queue.list_failed(limit=5)
    payload['queue'] = queue
    payload['worker_heartbeat_at'] = await storage.meta.get(WORKER_HEARTBEAT_KEY)
    return payload


@router.get('/settings/tasks')
async def list_sync_tasks(
    limit: int = 5,
    detail: bool = False,
    storage: PostgresStorage = Depends(get_storage),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """
    获取同步任务列表。
    """
    tasks = await storage.sync_jobs.list_jobs(limit=max(int(limit), 1))
    formatter = (lambda task: task.to_dict()) if detail else (lambda task: task.to_summary_dict())
    return {
        'tasks': [formatter(task) for task in tasks],
    }


@router.get('/settings/tasks/{task_id}')
async def get_sync_task(
    task_id: str,
    storage: PostgresStorage = Depends(get_storage),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """
    获取指定同步任务的进度与状态。
    """
    state = await storage.sync_jobs.get_job(task_id)
    if not state:
        raise ApiError('Task not found', status=404)
    return state.to_dict()


@router.post('/settings/tasks/{task_id}/cancel')
async def cancel_sync_task(
    task_id: str,
    storage: PostgresStorage = Depends(get_storage),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """Cancel a running or queued sync task."""
    async with storage.transaction():
        cancelled = await storage.sync_jobs.cancel_job(task_id)
    if not cancelled:
        raise ApiError('Task not found or not in a cancellable state', status=404)
    request_sync_cancel()
    state = await storage.sync_jobs.get_job(task_id)
    return state.to_dict() if state else {}


@router.get('/settings')
async def get_sync_settings(
    storage: PostgresStorage = Depends(get_storage),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """
    获取当前的同步配置设置。

    Returns:
        dict: 同步设置，包括启用状态、间隔、邮件配置等。
    """
    payload = await load_sync_settings(storage)
    payload['email'] = await get_email_settings(storage)
    return payload


@router.patch('/settings')
async def update_sync_settings(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(get_storage),
    scheduler: SyncScheduler | None = Depends(get_sync_scheduler),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """
    更新同步设置。

    Args:
        body (dict): 需要更新的设置 (enabled, interval_minutes, email 等)。

    Returns:
        dict: 更新后的设置。
    """
    updates: dict[str, Any] = {}
    if 'enabled' in body:
        updates['enabled'] = bool(body['enabled'])
    if 'interval_minutes' in body:
        updates['interval_minutes'] = max(int(body['interval_minutes']), 1)
    if 'window_start_hour' in body:
        updates['window_start_hour'] = min(max(int(body['window_start_hour']), 0), 23)
    if 'window_end_hour' in body:
        updates['window_end_hour'] = min(max(int(body['window_end_hour']), 0), 24)
    if 'sleep_seconds' in body:
        updates['sleep_seconds'] = float(body['sleep_seconds'])
    if 'download_content' in body:
        updates['download_content'] = bool(body['download_content'])
    if 'download_images' in body:
        updates['download_images'] = bool(body['download_images'])
    if 'skip_minutes' in body:
        updates['skip_minutes'] = max(int(body['skip_minutes']), 0)
    if 'alert_enabled' in body:
        updates['alert_enabled'] = bool(body['alert_enabled'])
    if 'alert_email' in body:
        updates['alert_email'] = str(body['alert_email']).strip()
    settings = await save_sync_settings(storage, updates)
    email_updates: dict[str, Any] = {}
    email_body = body.get('email')
    if isinstance(email_body, dict):
        for key in (
            'smtp_host',
            'smtp_port',
            'smtp_user',
            'smtp_password',
            'smtp_tls',
            'from_email',
        ):
            if key in email_body:
                email_updates[key] = email_body[key]
    if email_updates:
        settings['email'] = await set_email_settings(storage, email_updates)
    else:
        settings['email'] = await get_email_settings(storage)
    if settings.get('enabled') and scheduler:
        scheduler.trigger()
    return settings


@router.get('/settings/preferences')
async def get_preferences(
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Return the caller's personal reading preferences."""
    preferences = await storage.users.get_preferences(user.id)
    return {
        'timezone': user.timezone,
        'article_exclude_keywords': str(preferences.get('article_exclude_keywords') or ''),
    }


@router.patch('/settings/preferences')
async def update_preferences(
    body: dict[str, Any] = Body(default={}),
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Update the caller's personal reading preferences.

    Global sync cadence, SMTP and site settings live behind the admin-only
    ``PATCH /api/settings``; these are the knobs that only affect one reader.
    """
    async with storage.transaction():
        if 'article_exclude_keywords' in body:
            await storage.users.set_preferences(
                user.id,
                {'article_exclude_keywords': str(body['article_exclude_keywords'] or '')},
            )
        if 'timezone' in body:
            await storage.users.set_timezone(user.id, str(body['timezone']))

    refreshed = await storage.users.get(user.id)
    preferences = await storage.users.get_preferences(user.id)
    return {
        'timezone': refreshed.timezone if refreshed else user.timezone,
        'article_exclude_keywords': str(preferences.get('article_exclude_keywords') or ''),
    }


@router.post('/settings/test-email')
async def send_sync_test_email(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(get_storage),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """
    发送测试邮件，使用当前或传入的 SMTP 配置。
    """
    to_email = str(body.get('to_email') or '').strip()
    if not to_email:
        settings = await load_sync_settings(storage)
        to_email = str(settings.get('alert_email') or '').strip()
    if not to_email:
        raise ApiError('to_email is required')

    email_settings = await get_email_settings(storage)
    email_body = body.get('email')
    if isinstance(email_body, dict):
        if 'smtp_host' in email_body:
            email_settings['smtp_host'] = str(email_body.get('smtp_host') or '').strip()
        if 'smtp_port' in email_body:
            try:
                email_settings['smtp_port'] = max(int(email_body.get('smtp_port')), 1)
            except (TypeError, ValueError) as exc:
                raise ApiError('Invalid smtp_port') from exc
        if 'smtp_user' in email_body:
            email_settings['smtp_user'] = str(email_body.get('smtp_user') or '').strip()
        if 'smtp_password' in email_body:
            email_settings['smtp_password'] = str(email_body.get('smtp_password') or '')
        if 'smtp_tls' in email_body:
            email_settings['smtp_tls'] = bool(email_body.get('smtp_tls'))
        if 'from_email' in email_body:
            email_settings['from_email'] = str(email_body.get('from_email') or '').strip()

    smtp_host = str(email_settings.get('smtp_host') or '').strip()
    if not smtp_host:
        raise ApiError('smtp_host is required')

    sent_at = datetime.now(UTC).isoformat()
    subject = 'Hippo test email'
    message = f'This is a test email from Hippo.\n\nSent at (UTC): {sent_at}\nTo: {to_email}\n'
    started_at = time_module.monotonic()
    logger.info(
        'Sending test email: to=%s smtp_host=%s smtp_port=%s smtp_tls=%s',
        to_email,
        smtp_host,
        email_settings.get('smtp_port'),
        bool(email_settings.get('smtp_tls')),
    )
    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                send_email,
                email_settings,
                to_email=to_email,
                subject=subject,
                body=message,
            ),
            timeout=20,
        )
    except TimeoutError as exc:
        elapsed_ms = int((time_module.monotonic() - started_at) * 1000)
        logger.warning(
            'Test email timeout: to=%s smtp_host=%s elapsed_ms=%s',
            to_email,
            smtp_host,
            elapsed_ms,
        )
        raise ApiError('Test email timed out. Please verify SMTP host/port/TLS.') from exc
    except Exception as exc:
        elapsed_ms = int((time_module.monotonic() - started_at) * 1000)
        logger.warning(
            'Failed to send test email: to=%s smtp_host=%s elapsed_ms=%s error=%s',
            to_email,
            smtp_host,
            elapsed_ms,
            exc,
        )
        raise ApiError(f'Failed to send test email: {exc}') from exc
    elapsed_ms = int((time_module.monotonic() - started_at) * 1000)
    logger.info(
        'Test email sent: to=%s smtp_host=%s elapsed_ms=%s',
        to_email,
        smtp_host,
        elapsed_ms,
    )

    return {'status': 'sent', 'to_email': to_email}


@router.post('/settings/run', status_code=status.HTTP_202_ACCEPTED)
async def run_sync(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """
    手动触发同步操作。

    Args:
        body (dict): 可选的 'group_id' 用于同步指定分组。

    Returns:
        dict: 触发操作的状态。
    """
    group_id = body.get('group_id')
    raw_biz_list = body.get('biz_list')
    biz_list: list[str] | None = None
    if group_id is not None:
        try:
            group_id = int(group_id)
        except (TypeError, ValueError) as exc:
            raise ApiError('Invalid group_id') from exc
        row = await fetchone_row(
            storage,
            'SELECT id FROM account_groups WHERE id = %s AND user_id = %s',
            [group_id, user.id],
            normalize=_normalize_record,
        )
        if not row:
            raise ApiError('Group not found', status=404)
    if raw_biz_list is not None:
        if not isinstance(raw_biz_list, list):
            raise ApiError('Invalid biz_list')
        normalized_biz_list: list[str] = []
        seen_biz: set[str] = set()
        for value in raw_biz_list:
            biz = str(value or '').strip()
            if not biz or biz in seen_biz:
                continue
            normalized_biz_list.append(biz)
            seen_biz.add(biz)
        if not normalized_biz_list:
            raise ApiError('Invalid biz_list')
        known_biz = {account.biz for account in await storage.accounts.list_followed_accounts(user.id)}
        missing_biz = [biz for biz in normalized_biz_list if biz not in known_biz]
        if missing_biz:
            raise ApiError(f'Account not found: {missing_biz[0]}', status=404)
        biz_list = normalized_biz_list
    async with storage.transaction():
        task_state = await storage.sync_jobs.create_job(
            trigger_type='manual',
            group_id=group_id,
            biz_list=biz_list,
        )
    response: dict[str, Any] = {
        'status': task_state.status,
        'task_id': task_state.task_id,
    }
    if group_id is not None:
        response['group_id'] = group_id
    if biz_list is not None:
        response['biz_list'] = biz_list
    return response
