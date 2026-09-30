"""Admin console: user administration.

Every mutation here is written to the audit trail. Role changes and session
revocation are the two operations that can lock a user out, so they are the
ones an incident review will look for.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, Depends, Request, status

from ...exceptions import ApiError
from ...models import User
from ...security import hash_password
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
