"""Admin console: user administration.

Every mutation here is written to the audit trail. Role changes and session
revocation are the two operations that can lock a user out, so they are the
ones an incident review will look for.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Query, Request, status

from ...config import WEB_SERVICE, get_settings
from ...exceptions import ApiError
from ...media.asr import health_url
from ...media_settings import (
    ASR_TIERS,
    INSTANCE_CREDENTIALS,
    get_media_settings,
    instance_credential,
    resolve_proxy,
    set_media_settings,
)
from ...models import User
from ...repositories.llm import mask_key
from ...security import hash_password
from ...site_settings import get_site_settings, set_site_settings
from ...sources.registry import source_names
from ...storage import PostgresStorage
from ..deps import client_ip, get_storage, require_admin
from ..schemas.admin import (
    AdminUserCreatedOut,
    AdminUserListOut,
    AdminUserUpdatedOut,
    AuditActionsOut,
    AuditPageOut,
    LogTailOut,
    MediaSettingsOut,
    MediaSettingsUpdateRequest,
    MediaTestOut,
    PasswordResetRequest,
    SignozLinkOut,
    SiteSettingsOut,
    SiteSettingsUpdateRequest,
    SourceOverviewListOut,
    UserCreateRequest,
    UserUpdateRequest,
)

router = APIRouter()

_MIN_PASSWORD_LENGTH = 8


def _target_id(user_id: str) -> int:
    try:
        return int(user_id)
    except (TypeError, ValueError) as exc:
        raise ApiError('Invalid user id', status=400) from exc


def _guard_self(actor: User, target_id: int, action: str) -> None:
    """Refuse operations that would strip the acting admin's own access."""
    if actor.id == target_id:
        raise ApiError(f'不能对自己执行{action}', status=400)


@router.get('/admin/user', response_model=AdminUserListOut)
async def list_users(
    _: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """List every account with its verification, activity and session state."""
    return {'items': await storage.users.list_with_stats()}


@router.post('/admin/user', status_code=status.HTTP_201_CREATED, response_model=AdminUserCreatedOut)
async def create_user(
    request: Request,
    body: UserCreateRequest,
    actor: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Create an account. The password is set directly, so e-mail is optional."""
    username = body.username.strip()
    password = body.password
    email = (body.email or '').strip() or None
    role = body.role
    # The admin vouches for the account and no verification mail is sent here,
    # so a fresh account has to be usable right away.
    email_verified = body.email_verified

    if not username:
        raise ApiError('用户名不能为空', status=400)
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise ApiError(f'密码至少 {_MIN_PASSWORD_LENGTH} 位', status=400)
    if await storage.users.count() and await _username_taken(storage, username):
        raise ApiError('用户名已存在', status=409)
    if email and await storage.users.get_by_email(email):
        raise ApiError('邮箱已被使用', status=409)

    password_hash = await asyncio.to_thread(hash_password, password)
    async with storage.transaction():
        user = await storage.users.create(
            username=username,
            password_hash=password_hash,
            email=email,
            role=role,
            email_verified=email_verified,
        )
        await storage.audit.record(
            actor.id,
            'admin.user_created',
            target=username,
            detail={'role': role, 'email': email},
            ip=client_ip(request),
        )
    return {'id': user.id, 'username': user.username, 'role': user.role}


@router.patch('/admin/user/{user_id}', response_model=AdminUserUpdatedOut)
async def update_user(
    user_id: str,
    request: Request,
    body: UserUpdateRequest,
    actor: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Toggle disabled state, change role, verify an e-mail or set a timezone."""
    target_id = _target_id(user_id)
    target = await storage.users.get(target_id)
    if target is None:
        raise ApiError('用户不存在', status=404)

    updates = body.model_dump(exclude_unset=True)
    changes: dict[str, Any] = {}
    async with storage.transaction():
        if 'is_disabled' in updates:
            disabled = bool(updates['is_disabled'])
            if disabled:
                _guard_self(actor, target_id, '禁用')
                await storage.sessions.revoke_all_for_user(target_id)
            await storage.users.set_disabled(target_id, disabled)
            changes['is_disabled'] = disabled

        if 'role' in updates:
            role = str(updates['role'])
            if role != 'admin':
                _guard_self(actor, target_id, '降级')
            await storage.users.set_role(target_id, role)
            changes['role'] = role

        if 'email_verified' in updates:
            verified = bool(updates['email_verified'])
            await storage.users.set_email_verified(target_id, verified)
            changes['email_verified'] = verified

        if 'timezone' in updates:
            timezone = str(updates['timezone'])
            if not timezone:
                raise ApiError('时区不能为空', status=400)
            await storage.users.set_timezone(target_id, timezone)
            changes['timezone'] = timezone

        if not changes:
            raise ApiError('没有需要更新的字段', status=400)

        await storage.audit.record(
            actor.id,
            'admin.user_updated',
            target=target.username,
            detail=changes,
            ip=client_ip(request),
        )
    return {'id': target_id, 'changes': changes}


@router.post('/admin/user/{user_id}/password', status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    user_id: str,
    request: Request,
    body: PasswordResetRequest,
    actor: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> None:
    """Set a new password and drop every existing session for that account."""
    target_id = _target_id(user_id)
    password = body.password
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise ApiError(f'密码至少 {_MIN_PASSWORD_LENGTH} 位', status=400)
    target = await storage.users.get(target_id)
    if target is None:
        raise ApiError('用户不存在', status=404)

    password_hash = await asyncio.to_thread(hash_password, password)
    async with storage.transaction():
        await storage.users.set_password(target_id, password_hash)
        await storage.sessions.revoke_all_for_user(target_id)
        await storage.audit.record(
            actor.id,
            'admin.password_reset',
            target=target.username,
            ip=client_ip(request),
        )


@router.delete('/admin/user/{user_id}/session', status_code=status.HTTP_204_NO_CONTENT)
async def revoke_sessions(
    user_id: str,
    request: Request,
    actor: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> None:
    """Drop every session for one account, forcing a fresh sign-in."""
    target_id = _target_id(user_id)
    target = await storage.users.get(target_id)
    if target is None:
        raise ApiError('用户不存在', status=404)
    async with storage.transaction():
        revoked = await storage.sessions.revoke_all_for_user(target_id)
        await storage.audit.record(
            actor.id,
            'admin.sessions_revoked',
            target=target.username,
            detail={'count': revoked},
            ip=client_ip(request),
        )


async def _username_taken(storage: PostgresStorage, username: str) -> bool:
    async with storage.transaction():
        return await storage.users.get_with_password(username) is not None


@router.get('/admin/site', response_model=SiteSettingsOut)
async def read_site_settings(
    _: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Instance-wide settings: what the site is called and whether it takes sign-ups."""
    return await get_site_settings(storage)


@router.patch('/admin/site', response_model=SiteSettingsOut)
async def update_site_settings(
    request: Request,
    body: SiteSettingsUpdateRequest,
    actor: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Merge site settings.

    Closing registration does not invalidate accounts that already exist; it
    only stops new ones from being created.
    """
    before = await get_site_settings(storage)
    settings = await set_site_settings(storage, body.model_dump(exclude_unset=True))
    changed = {key: value for key, value in settings.items() if before.get(key) != value}
    if changed:
        async with storage.transaction():
            await storage.audit.record(
                actor.id,
                'admin.site_settings_updated',
                detail=changed,
                ip=client_ip(request),
            )
    return settings


#: Characters a masked secret is rendered with. A submitted value containing one
#: is the round-tripped mask, not a new secret, and must not overwrite the stored
#: value with bullets.
_MASK_CHARACTERS = '•'


def _public_media_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Serialisable form: secrets are masked, never echoed whole."""
    public = dict(settings)
    for key in ('asr_api_key', 'bilibili_cookie', 'youtube_cookies'):
        public[key] = mask_key(str(settings.get(key) or ''))
        public[f'{key}_set'] = bool(str(settings.get(key) or '').strip())
    public['asr_endpoints'] = dict(settings.get('asr_endpoints') or {})
    return public


def _without_round_tripped_masks(body: dict[str, Any]) -> dict[str, Any]:
    """Drop secret fields that still hold the mask the console displayed."""
    cleaned: dict[str, Any] = {}
    for key, value in body.items():
        if isinstance(value, str) and _MASK_CHARACTERS in value:
            continue
        cleaned[key] = value
    return cleaned


@router.get('/admin/media', response_model=MediaSettingsOut)
async def read_media_settings(
    _: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Media sources and transcription backend, with secrets masked."""
    return _public_media_settings(await get_media_settings(storage))


@router.patch('/admin/media', response_model=MediaSettingsOut)
async def update_media_settings(
    request: Request,
    body: MediaSettingsUpdateRequest,
    actor: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Merge media settings; a field left at its mask keeps the stored value."""
    before = _public_media_settings(await get_media_settings(storage))
    submitted = _without_round_tripped_masks(body.model_dump(exclude_unset=True))
    settings = await set_media_settings(storage, submitted)
    after = _public_media_settings(settings)
    changed = {key: value for key, value in after.items() if before.get(key) != value}
    if changed:
        async with storage.transaction():
            await storage.audit.record(
                actor.id,
                'admin.media_settings_updated',
                detail=changed,
                ip=client_ip(request),
            )
    return after


#: Probe budget. The console wants a fast answer, not the slowest upstream.
_PROBE_TIMEOUT = 5.0


async def _probe_asr_tier(tier: str, settings: dict[str, Any]) -> dict[str, Any]:
    endpoint = str((settings.get('asr_endpoints') or {}).get(tier) or '').strip()
    if not endpoint:
        return {'tier': tier, 'ok': False, 'detail': '未配置端点'}
    url = health_url(endpoint)
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return {'tier': tier, 'ok': False, 'detail': f'无法连接 {url}：{exc}'}
    if response.status_code != 200:
        return {'tier': tier, 'ok': False, 'detail': f'{url} 返回 HTTP {response.status_code}'}
    try:
        payload = response.json()
    except ValueError:
        return {'tier': tier, 'ok': False, 'detail': f'{url} 没返回 JSON'}
    backend = str((payload or {}).get('backend') or '')
    return {'tier': tier, 'ok': True, 'detail': f'backend={backend}' if backend else 'ok'}


@router.post('/admin/media/test', response_model=MediaTestOut)
async def test_media_settings(
    _: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Probe every configured ASR tier so the console can show what is reachable.

    Source-specific probes (B站 / YouTube / feed) are added by the source
    commits, which is where the clients for them live.
    """
    settings = await get_media_settings(storage)
    probes = await asyncio.gather(*(_probe_asr_tier(tier, settings) for tier in ASR_TIERS))
    return {'asr': list(probes), 'ok': all(probe['ok'] for probe in probes)}


#: Default audit page size. Bounded above so one request cannot scan the table.
_AUDIT_PAGE_SIZE = 50
_MAX_AUDIT_PAGE_SIZE = 200


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ApiError(f'Invalid timestamp: {raw}', status=400) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@router.get('/admin/audit', response_model=AuditPageOut)
async def list_audit(
    action: str | None = None,
    user_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=_AUDIT_PAGE_SIZE, ge=1, le=_MAX_AUDIT_PAGE_SIZE),
    _: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Filtered, paginated audit trail."""
    filters = {
        'action': action or None,
        'user_id': _target_id(user_id) if user_id else None,
        'since': _parse_time(since),
        'until': _parse_time(until),
    }
    total = await storage.audit.count(**filters)
    items = await storage.audit.list(
        limit=page_size,
        offset=(page - 1) * page_size,
        **filters,
    )
    return {
        'items': items,
        'page': page,
        'page_size': page_size,
        'total': total,
        'pages': max((total + page_size - 1) // page_size, 1),
    }


@router.get('/admin/audit/action', response_model=AuditActionsOut)
async def list_audit_actions(
    _: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Distinct actions present in the trail, for the filter dropdown."""
    return {'items': await storage.audit.actions()}


@router.get('/admin/log/tail', response_model=LogTailOut, response_model_exclude_none=True)
async def tail_log(
    lines: int | None = Query(default=None, ge=1, le=5000),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """Last N lines of the rotating file log.

    SignOz is the primary place to read logs; this is the fallback for when it
    is unreachable and someone needs to see what just happened.
    """
    limit = min(lines or get_settings().logging.tail_lines, 5000)
    path = _log_file_path()
    if path is None:
        return {'available': False, 'reason': '未配置文件日志（[logging] file）', 'lines': []}
    if not path.exists():
        return {'available': False, 'reason': f'日志文件不存在: {path}', 'lines': []}

    # ponytail: read the tail by seeking backwards; fine for the rotation sizes
    # here, swap for a streaming reader if log files ever grow to hundreds of MB.
    with path.open('rb') as handle:
        handle.seek(0, 2)
        size = handle.tell()
        block = min(size, limit * 512)
        handle.seek(max(size - block, 0))
        content = handle.read().decode('utf-8', errors='replace')
    captured = content.splitlines()[-limit:]
    return {
        'available': True,
        'path': str(path),
        'size': size,
        'lines': captured,
    }


@router.get('/admin/log/link', response_model=SignozLinkOut, response_model_exclude_none=True)
async def signoz_link(
    minutes: int = Query(default=60, ge=1, le=1440),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """Deep link into SignOz for the last ``minutes``.

    The window is padded a little so the entry that prompted the investigation
    is inside it once the browser lands.
    """
    signoz_url = get_settings().web.signoz_url
    if not signoz_url:
        return {'available': False, 'reason': '未配置 [web] signoz_url'}
    until = datetime.now(UTC)
    since = until - timedelta(minutes=minutes)
    start_ms = int(since.timestamp() * 1000)
    end_ms = int(until.timestamp() * 1000)
    url = f'{signoz_url}/logs?&startTime={start_ms}&endTime={end_ms}&service.name=hippo'
    return {'available': True, 'url': url, 'since': since.isoformat(), 'until': until.isoformat()}


def _log_file_path() -> Path | None:
    target = get_settings().logging.file_for(WEB_SERVICE)
    return Path(target) if target else None


@router.get('/admin/source', response_model=SourceOverviewListOut)
async def source_overview(
    _: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Every source hippo knows about, with what the console needs to show it.

    The source list is assembled here rather than in the browser: which sources
    exist, which accept a credential and which one owns the WeChat daemon are
    facts about the backend, and the page should not keep a copy of them.
    """
    settings = await get_media_settings(storage)
    overview = {row['source']: row for row in await storage.accounts.overview_by_source()}
    backlog = await storage.media_jobs.stats_by_source()

    sources: list[dict[str, Any]] = []
    for source in source_names():
        row = overview.get(source, {})
        # 没订阅之前也要出现在列表里：它可能只是还没配凭证。
        sources.append(
            {
                'source': source,
                'channels': int(row.get('channels') or 0),
                'enabled_channels': int(row.get('enabled_channels') or 0),
                'followers': int(row.get('followers') or 0),
                'last_synced_at': row.get('last_synced_at'),
                'credential': (
                    {
                        'field': INSTANCE_CREDENTIALS[source],
                        'configured': bool(instance_credential(settings, source)),
                    }
                    if source in INSTANCE_CREDENTIALS
                    else None
                ),
                'proxy': resolve_proxy(settings, source),
                'backlog': backlog.get(source),
            }
        )
    return {
        'sources': sources,
        'media_enabled': bool(settings.get('enabled')),
    }
