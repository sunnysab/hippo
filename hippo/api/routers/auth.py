"""User authentication: password login backed by server-side sessions."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import APIRouter, Body, Depends, Request, Response, status

from ...exceptions import ApiError
from ...logger import get_logger
from ...models import User
from ...security import hash_password, verify_password
from ...storage import PostgresStorage
from ..deps import SESSION_COOKIE, client_ip, current_user, get_storage
from ..ratelimit import SlidingWindowLimiter

logger = get_logger(__name__)
router = APIRouter()

#: Failed attempts per IP. Keyed before the password check so brute force is
#: throttled even when the username does not exist.
login_limiter = SlidingWindowLimiter(limit=10, window_seconds=300)

_MIN_PASSWORD_LENGTH = 8


def cookie_secure() -> bool:
    """``Secure`` must be off when serving plain HTTP in development."""
    raw = os.environ.get('HIPPO_COOKIE_SECURE', '0')
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def _set_session_cookie(response: Response, token: str) -> None:
    from ...repositories.session import session_ttl

    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite='lax',
        secure=cookie_secure(),
        max_age=int(session_ttl().total_seconds()),
        path='/',
    )


@router.post('/auth/login', status_code=status.HTTP_204_NO_CONTENT)
async def login(
    request: Request,
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(get_storage),
) -> Response:
    """Sign in with username and password, setting the session cookie."""
    username = str(body.get('username') or '').strip()
    password = str(body.get('password') or '')
    ip = client_ip(request)
    limiter_key = ip or 'unknown'

    if not login_limiter.allow(limiter_key):
        raise ApiError('尝试过于频繁，请稍后再试', status=429)
    if not username or not password:
        raise ApiError('用户名和密码不能为空', status=400)

    credentials = await storage.users.get_with_password(username)
    authenticated = False
    if credentials is not None:
        candidate, password_hash = credentials
        password_ok = await asyncio.to_thread(verify_password, password, password_hash)
        if password_ok and candidate.is_disabled:
            raise ApiError('账号已被禁用', status=403)
        if password_ok and not candidate.email_verified:
            raise ApiError('邮箱尚未验证，请先完成邮箱验证', status=403)
        authenticated = password_ok

    if not authenticated:
        async with storage.transaction():
            await storage.audit.record(None, 'auth.login_failed', target=username, ip=ip)
        raise ApiError('用户名或密码错误', status=401)

    user = credentials[0]
    async with storage.transaction():
        token = await storage.sessions.issue(
            user.id, user_agent=request.headers.get('user-agent'), ip=ip
        )
        await storage.audit.record(user.id, 'auth.login', target=user.username, ip=ip)
    login_limiter.reset(limiter_key)

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _set_session_cookie(response, token)
    return response


@router.post('/auth/logout', status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    storage: PostgresStorage = Depends(get_storage),
) -> Response:
    """Revoke the current session."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        async with storage.transaction():
            await storage.sessions.revoke(token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path='/')
    return response


@router.get('/auth/me')
async def me(user: User = Depends(current_user)) -> dict[str, Any]:
    """Return the signed-in user."""
    return {
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'email_verified': user.email_verified,
        'role': user.role,
        'timezone': user.timezone,
    }


@router.post('/auth/password', status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    request: Request,
    body: dict[str, Any] = Body(default={}),
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> Response:
    """Change the password and rotate every session, including this one."""
    current = str(body.get('current_password') or '')
    new_password = str(body.get('new_password') or '')
    if len(new_password) < _MIN_PASSWORD_LENGTH:
        raise ApiError(f'新密码至少 {_MIN_PASSWORD_LENGTH} 位', status=400)

    credentials = await storage.users.get_with_password(user.username)
    if credentials is None or not await asyncio.to_thread(verify_password, current, credentials[1]):
        raise ApiError('当前密码不正确', status=400)

    new_hash = await asyncio.to_thread(hash_password, new_password)
    async with storage.transaction():
        await storage.users.set_password(user.id, new_hash)
        # Changing the password invalidates every existing session, then hands
        # the caller a fresh one so they are not logged out of this tab.
        await storage.sessions.revoke_all_for_user(user.id)
        token = await storage.sessions.issue(
            user.id, user_agent=request.headers.get('user-agent'), ip=client_ip(request)
        )
        await storage.audit.record(
            user.id, 'auth.password_changed', target=user.username, ip=client_ip(request)
        )

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _set_session_cookie(response, token)
    return response


@router.get('/auth/session')
async def my_sessions(
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """List the current user's active sessions."""
    sessions = await storage.sessions.list_for_user(user.id)
    for session in sessions:
        for key in ('created_at', 'expires_at'):
            value = session.get(key)
            session[key] = value.isoformat() if value else None
        # Never expose the token hash to the client.
        session.pop('token_hash', None)
    return {'sessions': sessions}


@router.post('/auth/session/revoke', status_code=status.HTTP_204_NO_CONTENT)
async def revoke_my_sessions(
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> Response:
    """Sign out everywhere."""
    async with storage.transaction():
        await storage.sessions.revoke_all_for_user(user.id)
        await storage.audit.record(user.id, 'auth.sessions_revoked', target=user.username)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path='/')
    return response
