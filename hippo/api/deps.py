"""Shared FastAPI dependencies and request-parameter helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Request

from ..exceptions import ApiError
from ..models import User
from ..storage import PostgresStorage, open_storage
from ..sync_scheduler import SyncScheduler

#: Name of the cookie carrying the opaque session token.
SESSION_COOKIE = 'hippo_session'


def parse_int(value: str | None) -> int | None:
    if value is None or value == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(f'Invalid integer: {value}') from exc


async def get_storage() -> AsyncIterator[PostgresStorage]:
    async with open_storage() as storage:
        yield storage


def get_sync_scheduler(request: Request) -> SyncScheduler | None:
    return getattr(request.app.state, 'sync_scheduler', None)


def client_ip(request: Request) -> str | None:
    """Best-effort client address, honouring one layer of reverse proxy."""
    forwarded = request.headers.get('x-forwarded-for')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.client.host if request.client else None


async def current_user_optional(
    request: Request,
    storage: PostgresStorage = Depends(get_storage),
) -> User | None:
    """Resolve the session cookie, returning ``None`` when absent or expired."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return await storage.sessions.resolve(token)


async def current_user(user: User | None = Depends(current_user_optional)) -> User:
    """Require a signed-in, enabled user."""
    if user is None:
        raise ApiError('未登录', status=401)
    return user


async def require_admin(user: User = Depends(current_user)) -> User:
    """Require the admin role."""
    if user.role != 'admin':
        raise ApiError('需要管理员权限', status=403)
    return user


def parse_group_ids(group_ids: str | None, group_id: int | None = None) -> list[int] | None:
    """Parse group_ids from query parameter, with backward-compatible group_id fallback."""
    if group_ids:
        try:
            ids = [int(x.strip()) for x in group_ids.split(',') if x.strip()]
            return ids or None
        except ValueError:
            return None
    if group_id is not None:
        return [group_id]
    return None


__all__ = [
    'SESSION_COOKIE',
    'client_ip',
    'current_user',
    'current_user_optional',
    'get_storage',
    'get_sync_scheduler',
    'parse_group_ids',
    'parse_int',
    'require_admin',
]
