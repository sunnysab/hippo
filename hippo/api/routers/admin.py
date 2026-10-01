"""Admin console: user administration.

Every mutation here is written to the audit trail. Role changes and session
revocation are the two operations that can lock a user out, so they are the
ones an incident review will look for.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request, status

from ...config import LOG_TAIL_LINES, SIGNOZ_URL
from ...exceptions import ApiError
from ...models import User
from ...security import hash_password
from ...site_settings import get_site_settings, set_site_settings
from ...storage import PostgresStorage
from ..deps import client_ip, get_storage, require_admin

router = APIRouter()

_MIN_PASSWORD_LENGTH = 8
_ROLES = {'user', 'admin'}


def _target_id(user_id: str) -> int:
    try:
        return int(user_id)
    except (TypeError, ValueError) as exc:
        raise ApiError('Invalid user id', status=400) from exc


def _guard_self(actor: User, target_id: int, action: str) -> None:
    """Refuse operations that would strip the acting admin's own access."""
    if actor.id == target_id:
        raise ApiError(f'不能对自己执行{action}', status=400)


@router.get('/admin/user')
async def list_users(
    _: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """List every account with its verification, activity and session state."""
    return {'items': await storage.users.list_with_stats()}


@router.post('/admin/user', status_code=status.HTTP_201_CREATED)
async def create_user(
    request: Request,
    body: dict[str, Any] = Body(default={}),
    actor: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Create an account. The password is set directly, so e-mail is optional."""
    username = str(body.get('username') or '').strip()
    password = str(body.get('password') or '')
    email = str(body.get('email') or '').strip() or None
    role = str(body.get('role') or 'user')
    email_verified = bool(body.get('email_verified', email is not None and False))

    if not username:
        raise ApiError('用户名不能为空', status=400)
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise ApiError(f'密码至少 {_MIN_PASSWORD_LENGTH} 位', status=400)
    if role not in _ROLES:
        raise ApiError(f'未知角色: {role}', status=400)
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


@router.patch('/admin/user/{user_id}')
async def update_user(
    user_id: str,
    request: Request,
    body: dict[str, Any] = Body(default={}),
    actor: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Toggle disabled state, change role, verify an e-mail or set a timezone."""
    target_id = _target_id(user_id)
    target = await storage.users.get(target_id)
    if target is None:
        raise ApiError('用户不存在', status=404)

    changes: dict[str, Any] = {}
    async with storage.transaction():
        if 'is_disabled' in body:
            disabled = bool(body['is_disabled'])
            if disabled:
                _guard_self(actor, target_id, '禁用')
                await storage.sessions.revoke_all_for_user(target_id)
            await storage.users.set_disabled(target_id, disabled)
            changes['is_disabled'] = disabled

        if 'role' in body:
            role = str(body['role'])
            if role not in _ROLES:
                raise ApiError(f'未知角色: {role}', status=400)
            if role != 'admin':
                _guard_self(actor, target_id, '降级')
            await storage.users.set_role(target_id, role)
            changes['role'] = role

        if 'email_verified' in body:
            verified = bool(body['email_verified'])
            await storage.users.set_email_verified(target_id, verified)
            changes['email_verified'] = verified

        if 'timezone' in body:
            timezone = str(body['timezone'])
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
    body: dict[str, Any] = Body(default={}),
    actor: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> None:
    """Set a new password and drop every existing session for that account."""
    target_id = _target_id(user_id)
    password = str(body.get('password') or '')
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


@router.get('/admin/site')
async def read_site_settings(
    _: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Instance-wide settings: what the site is called and whether it takes sign-ups."""
    return await get_site_settings(storage)


@router.patch('/admin/site')
async def update_site_settings(
    request: Request,
    body: dict[str, Any] = Body(default={}),
    actor: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Merge site settings.

    Closing registration does not invalidate accounts that already exist; it
    only stops new ones from being created.
    """
    before = await get_site_settings(storage)
    settings = await set_site_settings(storage, body)
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


@router.get('/admin/audit')
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


@router.get('/admin/audit/action')
async def list_audit_actions(
    _: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Distinct actions present in the trail, for the filter dropdown."""
    return {'items': await storage.audit.actions()}


@router.get('/admin/log/tail')
async def tail_log(
    lines: int = Query(default=LOG_TAIL_LINES, ge=1, le=5000),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """Last N lines of the rotating file log.

    SignOz is the primary place to read logs; this is the fallback for when it
    is unreachable and someone needs to see what just happened.
    """
    path = _log_file_path()
    if path is None:
        return {'available': False, 'reason': '未配置文件日志（HIPPO_LOG_FILE）', 'lines': []}
    if not path.exists():
        return {'available': False, 'reason': f'日志文件不存在: {path}', 'lines': []}

    # ponytail: read the tail by seeking backwards; fine for the rotation sizes
    # here, swap for a streaming reader if log files ever grow to hundreds of MB.
    with path.open('rb') as handle:
        handle.seek(0, 2)
        size = handle.tell()
        block = min(size, lines * 512)
        handle.seek(max(size - block, 0))
        content = handle.read().decode('utf-8', errors='replace')
    captured = content.splitlines()[-lines:]
    return {
        'available': True,
        'path': str(path),
        'size': size,
        'lines': captured,
    }


@router.get('/admin/log/link')
async def signoz_link(
    minutes: int = Query(default=60, ge=1, le=1440),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """Deep link into SignOz for the last ``minutes``.

    The window is padded a little so the entry that prompted the investigation
    is inside it once the browser lands.
    """
    if not SIGNOZ_URL:
        return {'available': False, 'reason': '未配置 HIPPO_SIGNOZ_URL'}
    until = datetime.now(UTC)
    since = until - timedelta(minutes=minutes)
    start_ms = int(since.timestamp() * 1000)
    end_ms = int(until.timestamp() * 1000)
    url = f'{SIGNOZ_URL}/logs?&startTime={start_ms}&endTime={end_ms}&service.name=hippo'
    return {'available': True, 'url': url, 'since': since.isoformat(), 'until': until.isoformat()}


def _log_file_path() -> Path | None:
    raw = os.environ.get('HIPPO_LOG_FILE')
    return Path(raw) if raw else None
