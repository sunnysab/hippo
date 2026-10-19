"""Sync, filter and mail settings endpoints."""

import asyncio
import time as time_module
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Request, status

from ...article_queries import _normalize_record
from ...emailer import get_email_settings, send_email, set_email_settings
from ...exceptions import ApiError
from ...logger import get_logger
from ...models import User
from ...repositories.llm import mask_key
from ...storage import PostgresStorage, fetchone_row, load_meta_json
from ...sync_core import request_sync_cancel
from ...sync_scheduler import SyncScheduler
from ...sync_settings import QUEUE_STATS_KEY, WORKER_HEARTBEAT_KEY
from ...sync_settings import get_sync_settings as load_sync_settings
from ...sync_settings import get_sync_status as load_sync_status
from ...sync_settings import set_sync_settings as save_sync_settings
from ...sync_types import SyncTaskState
from ..deps import current_user, get_storage, get_sync_scheduler, is_admin_session, require_admin
from ..schemas.settings import (
    EmailSettingsUpdate,
    EmailTestRequest,
    EmailTestResult,
    PreferencesOut,
    PreferencesUpdateRequest,
    SyncSettingsOut,
    SyncSettingsUpdateRequest,
    SyncStatusOut,
)
from ..schemas.sync import (
    SyncRunRequest,
    SyncRunResponse,
    SyncSiteRunRequest,
    SyncTaskDetail,
    SyncTaskListResponse,
)

logger = get_logger(__name__)
router = APIRouter(dependencies=[Depends(current_user)])

#: Secrets are rendered with the same mask the admin media page uses. A value
#: that still contains one is the round-tripped display value, not a new secret.
_MASK_CHARACTERS = '•'


def _public_email_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Serialisable form: the password is masked, never echoed whole."""
    public = dict(settings)
    public['smtp_password'] = mask_key(str(settings.get('smtp_password') or ''))
    public['smtp_password_set'] = bool(str(settings.get('smtp_password') or '').strip())
    return public


def _submitted_email_updates(email: EmailSettingsUpdate | None) -> dict[str, Any]:
    """The email fields the caller sent, minus an explicit null and a round-tripped password mask."""
    if email is None:
        return {}
    updates = {key: value for key, value in email.model_dump(exclude_unset=True).items() if value is not None}
    if isinstance(updates.get('smtp_password'), str) and _MASK_CHARACTERS in updates['smtp_password']:
        updates.pop('smtp_password')
    return updates


@router.get('/settings/status', response_model=SyncStatusOut)
async def sync_status(
    limit: int = 5,
    storage: PostgresStorage = Depends(get_storage),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """
    获取后台同步任务的状态。

    全站的同步状态、队列水位与 worker 心跳，只对管理员开放；普通用户看自己的
    任务即可（``GET /api/settings/tasks``）。

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


@router.get('/settings/tasks', response_model_exclude_unset=True)
async def list_sync_tasks(
    request: Request,
    limit: int = 5,
    detail: bool = False,
    source: str | None = None,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> SyncTaskListResponse:
    """List the caller's tasks, or all tasks for an administrator web session.

    API keys remain owner-scoped even when their owner has the admin role.
    ``source`` narrows either scope to that source.
    """
    tasks = await storage.sync_jobs.list_jobs(
        limit=max(int(limit), 1),
        source=source or None,
        requester_user_id=None if is_admin_session(request, user) else user.id,
    )
    formatter = (lambda task: task.to_dict()) if detail else (lambda task: task.to_summary_dict())
    return SyncTaskListResponse(tasks=[formatter(task) for task in tasks])


@router.get('/settings/tasks/{task_id}')
async def get_sync_task(
    task_id: str,
    request: Request,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> SyncTaskDetail:
    """Return one owned task; only an administrator web session may cross owners.

    Unknown and foreign tasks both return 404, including for admin-owned keys.
    """
    state = await storage.sync_jobs.get_job(task_id)
    if not state or (not is_admin_session(request, user) and state.requester_user_id != user.id):
        raise ApiError('Task not found', status=404)
    return SyncTaskDetail.model_validate(state.to_dict())


@router.post('/settings/tasks/{task_id}/cancel')
async def cancel_sync_task(
    task_id: str,
    request: Request,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> SyncTaskDetail:
    """Cancel a running or queued sync task.

    Ownership follows the read endpoints: an administrator web session may
    cancel any task, all other credentials only their own. A foreign task looks
    missing rather than forbidden.
    """
    state = await storage.sync_jobs.get_job(task_id)
    if not state or (not is_admin_session(request, user) and state.requester_user_id != user.id):
        raise ApiError('Task not found', status=404)
    async with storage.transaction():
        cancelled = await storage.sync_jobs.cancel_job(task_id)
    if not cancelled:
        raise ApiError('Task not found or not in a cancellable state', status=404)
    request_sync_cancel()
    refreshed = await storage.sync_jobs.get_job(task_id) or state
    return SyncTaskDetail.model_validate(refreshed.to_dict())


@router.get('/settings', response_model=SyncSettingsOut)
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
    payload['email'] = _public_email_settings(await get_email_settings(storage))
    return payload


@router.patch('/settings', response_model=SyncSettingsOut)
async def update_sync_settings(
    body: SyncSettingsUpdateRequest = Body(default_factory=SyncSettingsUpdateRequest),
    storage: PostgresStorage = Depends(get_storage),
    scheduler: SyncScheduler | None = Depends(get_sync_scheduler),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """
    更新同步设置。

    只写入请求里出现的字段；未知字段直接 422（`request.invalid`），不回退成
    “忽略后返回旧值”。`email` 子对象里的 `smtp_password` 是只写的：放回接口回显
    的掩码会被丢弃，不会把已存密码覆盖成一串圆点。

    Args:
        body (SyncSettingsUpdateRequest): 需要更新的设置（`enabled`、`interval_minutes`、`email` 等）。

    Returns:
        dict: 更新后的设置。
    """
    sent = body.model_fields_set
    updates: dict[str, Any] = {}
    if 'enabled' in sent and body.enabled is not None:
        updates['enabled'] = body.enabled
    if 'interval_minutes' in sent and body.interval_minutes is not None:
        updates['interval_minutes'] = body.interval_minutes
    if 'sleep_seconds' in sent and body.sleep_seconds is not None:
        updates['sleep_seconds'] = body.sleep_seconds
    if 'download_content' in sent and body.download_content is not None:
        updates['download_content'] = body.download_content
    if 'download_images' in sent and body.download_images is not None:
        updates['download_images'] = body.download_images
    if 'article_exclude_keywords' in sent and body.article_exclude_keywords is not None:
        updates['article_exclude_keywords'] = body.article_exclude_keywords
    if 'skip_minutes' in sent and body.skip_minutes is not None:
        updates['skip_minutes'] = body.skip_minutes
    if 'alert_enabled' in sent and body.alert_enabled is not None:
        updates['alert_enabled'] = body.alert_enabled
    if 'alert_email' in sent and body.alert_email is not None:
        updates['alert_email'] = body.alert_email.strip()
    settings = await save_sync_settings(storage, updates)
    email_updates = _submitted_email_updates(body.email)
    if email_updates:
        await set_email_settings(storage, email_updates)
    settings['email'] = _public_email_settings(await get_email_settings(storage))
    if settings.get('enabled') and scheduler:
        scheduler.trigger()
    return settings


@router.get('/settings/preferences', response_model=PreferencesOut)
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


@router.patch('/settings/preferences', response_model=PreferencesOut)
async def update_preferences(
    body: PreferencesUpdateRequest = Body(default_factory=PreferencesUpdateRequest),
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Update the caller's personal reading preferences.

    Global sync cadence, SMTP and site settings live behind the admin-only
    ``PATCH /api/settings``; these are the knobs that only affect one reader.
    Only the keys sent are written; an unknown key is a 422.
    """
    sent = body.model_fields_set
    async with storage.transaction():
        if 'article_exclude_keywords' in sent:
            await storage.users.set_preferences(
                user.id,
                {'article_exclude_keywords': str(body.article_exclude_keywords or '')},
            )
        if 'timezone' in sent and body.timezone is not None:
            await storage.users.set_timezone(user.id, str(body.timezone))

    refreshed = await storage.users.get(user.id)
    preferences = await storage.users.get_preferences(user.id)
    return {
        'timezone': refreshed.timezone if refreshed else user.timezone,
        'article_exclude_keywords': str(preferences.get('article_exclude_keywords') or ''),
    }


@router.post('/settings/test-email', response_model=EmailTestResult)
async def send_sync_test_email(
    body: EmailTestRequest = Body(default_factory=EmailTestRequest),
    storage: PostgresStorage = Depends(get_storage),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """
    发送测试邮件，使用当前或传入的 SMTP 配置。

    请求体里放回接口回显的密码掩码会被丢弃；`smtp_port` 非法直接 422。
    """
    to_email = str(body.to_email or '').strip()
    if not to_email:
        settings = await load_sync_settings(storage)
        to_email = str(settings.get('alert_email') or '').strip()
    if not to_email:
        raise ApiError('to_email is required')

    email_settings = await get_email_settings(storage)
    email_settings.update(_submitted_email_updates(body.email))

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
        raise ApiError(
            'Test email timed out. Please verify SMTP host/port/TLS.',
            status=504,
            code='settings.email_timeout',
        ) from exc
    except Exception as exc:
        elapsed_ms = int((time_module.monotonic() - started_at) * 1000)
        logger.warning(
            'Failed to send test email: to=%s smtp_host=%s elapsed_ms=%s error=%s',
            to_email,
            smtp_host,
            elapsed_ms,
            exc,
        )
        # The upstream text is the point of a test button, and the endpoint is
        # admin-only -- but the failure is the mail server's, not the caller's,
        # so it is a 502 rather than the 400 the default would give.
        raise ApiError(
            f'Failed to send test email: {exc}',
            status=502,
            code='settings.email_failed',
        ) from exc
    elapsed_ms = int((time_module.monotonic() - started_at) * 1000)
    logger.info(
        'Test email sent: to=%s smtp_host=%s elapsed_ms=%s',
        to_email,
        smtp_host,
        elapsed_ms,
    )

    return {'status': 'sent', 'to_email': to_email}


def _normalize_biz_list(raw_biz_list: list[str] | None) -> list[str] | None:
    """Trim, drop blanks and duplicates; reject an explicitly empty list.

    An empty ``biz_list`` must never widen into a whole-site run, so it is a 400.
    """
    if raw_biz_list is None:
        return None
    normalized: list[str] = []
    seen_biz: set[str] = set()
    for value in raw_biz_list:
        biz = str(value or '').strip()
        if not biz or biz in seen_biz:
            continue
        normalized.append(biz)
        seen_biz.add(biz)
    if not normalized:
        raise ApiError('Invalid biz_list')
    return normalized


async def _resolve_own_scope(
    storage: PostgresStorage,
    user: User,
    *,
    group_id: int | None,
    source: str | None,
    biz_list: list[str] | None,
) -> list[str]:
    """Resolve the caller's own subscriptions the filters select.

    The requested filters intersect with the caller's enabled subscriptions, so
    the result can never reach an account they do not follow; an empty result is
    a 404, never a silent whole-site run. Admins use the same rule here and the
    explicit site route for the catalogue.
    """
    followed = await storage.accounts.list_followed_accounts(user.id)
    enabled_biz = {account.biz for account in followed if not account.is_disabled}

    if group_id is not None:
        row = await fetchone_row(
            storage,
            'SELECT id FROM account_groups WHERE id = %s AND user_id = %s',
            [group_id, user.id],
            normalize=_normalize_record,
        )
        if not row:
            raise ApiError('Group not found', status=404)

    if biz_list is not None:
        missing_biz = [biz for biz in biz_list if biz not in enabled_biz]
        if missing_biz:
            raise ApiError(f'Account not found: {missing_biz[0]}', status=404)

    scoped = set(enabled_biz)
    if group_id is not None:
        scoped &= {account.biz for account in followed if account.group_id == group_id}
    if source is not None:
        scoped &= {account.biz for account in followed if (account.source or 'weixin') == source}
    if biz_list is not None:
        scoped &= set(biz_list)
    if not scoped:
        raise ApiError('No syncable subscription for the requested scope', status=404)
    return [account.biz for account in followed if account.biz in scoped]


def _state_scope(state: SyncTaskState) -> Literal['own', 'site']:
    """Scope a queued job covers, read back from what the row stored."""
    return 'site' if state.group_id is None and state.biz_list is None else 'own'


def _job_response(state: SyncTaskState) -> SyncRunResponse:
    return SyncRunResponse(
        task_id=state.task_id,
        status=state.status,
        scope=_state_scope(state),
        group_id=state.group_id,
        biz_list=list(state.biz_list) if state.biz_list else None,
        source=state.source,
    )


async def _queue_manual_job(
    storage: PostgresStorage,
    *,
    user: User,
    group_id: int | None,
    source: str | None,
    biz_list: list[str] | None,
) -> SyncRunResponse:
    """Queue a manual job, reconciling with an already-active one.

    ``create_job`` dedupes atomically per requester, so it may hand back a task
    queued by an earlier request. If that task covers the same scope it *is* this
    operation, and its real id/scope are echoed back. A different scope is a
    conflict (409) rather than a silent drop: the caller must know the second
    request did not queue anything.
    """
    async with storage.transaction():
        state = await storage.sync_jobs.create_job(
            trigger_type='manual',
            group_id=group_id,
            biz_list=biz_list,
            source=source,
            requester_user_id=user.id,
            dedupe=True,
        )
    requested = (group_id, source, tuple(biz_list) if biz_list else None)
    existing = (state.group_id, state.source, state.biz_list)
    if existing != requested:
        raise ApiError(
            f'Sync task {state.task_id} is already active for a different scope; '
            'wait for it to finish or cancel it first',
            status=409,
            code='sync.task_conflict',
        )
    return _job_response(state)


@router.post('/settings/run', status_code=status.HTTP_202_ACCEPTED)
async def run_sync(
    body: SyncRunRequest = Body(default_factory=SyncRunRequest),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> SyncRunResponse:
    """
    手动触发同步操作（只涉及调用者自己的订阅）。

    普通用户与管理员走同一条规则：`group_id` / `source` / `biz_list` 取交集，
    空请求表示「我的全部订阅」，绝不会变成全站同步；没有可同步的订阅返回 404。
    全站同步是管理员专用的 `POST /api/settings/admin/run`，不会随角色被同一按钮
    意外放大。

    同一个用户已有活动的手动任务时：作用域相同则返回该任务（`task_id`/`status`/`scope`
    均为旧任务真实值）；作用域不同则返回 409，不静默丢弃第二次操作。

    Args:
        body (SyncRunRequest): 可选的 'group_id'、'source'、'biz_list'。

    Returns:
        SyncRunResponse: 任务 id、状态与作用域（始终为 own）。
    """
    group_id = body.group_id
    source = (body.source or '').strip() or None
    normalized_biz_list = _normalize_biz_list(body.biz_list)
    biz_list = await _resolve_own_scope(
        storage,
        user,
        group_id=group_id,
        source=source,
        biz_list=normalized_biz_list,
    )
    return await _queue_manual_job(storage, user=user, group_id=group_id, source=source, biz_list=biz_list)


@router.post('/settings/admin/run', status_code=status.HTTP_202_ACCEPTED)
async def run_site_sync(
    body: SyncSiteRunRequest = Body(default_factory=SyncSiteRunRequest),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(require_admin),
) -> SyncRunResponse:
    """
    管理员显式触发全站（整个目录）同步。

    与普通用户的 `POST /api/settings/run` 分开：全站语义只在这里出现，不依赖调用者
    角色去改变同一条路径的含义。`source` 可把范围限定到某个来源。

    Args:
        body (SyncSiteRunRequest): 可选的 'source'。

    Returns:
        SyncRunResponse: 任务 id、状态与作用域（site）。
    """
    source = (body.source or '').strip() or None
    return await _queue_manual_job(storage, user=user, group_id=None, source=source, biz_list=None)
