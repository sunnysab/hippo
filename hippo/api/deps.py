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

#: Headers an API key may arrive in. The bearer form is what OpenAI-style clients
#: send; ``X-API-Key`` is what curl users reach for.
API_KEY_HEADERS = ('authorization', 'x-api-key')

#: Paths an API key may never reach. Administration and credential handling are
#: obvious; key management itself matters just as much, or a leaked key could mint
#: itself a wider one.
API_KEY_FORBIDDEN_PREFIXES = (
    '/api/admin',
    '/api/apikey',
    '/api/auth',
    '/api/login',
    '/api/source/credential',
)

#: Methods that only read. Anything else needs the ``write`` scope, which the
#: console does not issue yet.
_SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})


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
    """Caller address, or ``None`` when it cannot be established.

    ``X-Forwarded-For`` is only believed when the request arrived with no peer
    address at all, which is what a Unix-socket listener reports; Caddy fronts
    the socket and sets the header there. Over TCP the header is
    caller-controlled, so the peer address wins: a direct connection cannot
    claim someone else's address and slip past a per-address rate limit.
    """
    if request.client is None:
        forwarded = request.headers.get('x-forwarded-for')
        return forwarded.split(',')[0].strip() or None if forwarded else None
    return request.client.host or None


def _api_key_from_request(request: Request) -> str | None:
    authorization = request.headers.get('authorization') or ''
    if authorization.lower().startswith('bearer '):
        return authorization[7:].strip() or None
    raw = request.headers.get('x-api-key')
    return raw.strip() if raw else None


async def api_key_user(request: Request, storage: PostgresStorage) -> User | None:
    """Resolve an API key, enforcing what a key may and may not do.

    Raises rather than returning ``None`` for a key that was presented but is
    unusable: silently falling back to anonymous would turn a revoked key into a
    confusing 401 from some deeper dependency.
    """
    secret = _api_key_from_request(request)
    if not secret:
        return None
    path = request.url.path
    if any(path.startswith(prefix) for prefix in API_KEY_FORBIDDEN_PREFIXES):
        raise ApiError('API Key 不能用于管理接口，请用网页登录', status=403)
    key = await storage.api_keys.authenticate(secret)
    if key is None:
        raise ApiError('API Key 无效或已吊销', status=401)
    scopes = tuple(key.get('scopes') or ())
    if request.method not in _SAFE_METHODS and 'write' not in scopes:
        raise ApiError('这个 API Key 只有只读权限', status=403)
    await storage.api_keys.touch(int(key['id']), client_ip(request))
    await storage.commit()
    user = await storage.users.get(int(key['user_id']))
    if user is None or user.is_disabled:
        raise ApiError('API Key 对应的账号不可用', status=403)
    request.state.api_key = {'id': int(key['id']), 'name': key.get('name'), 'scopes': list(scopes)}
    return user


async def current_user_optional(
    request: Request,
    storage: PostgresStorage = Depends(get_storage),
) -> User | None:
    """Resolve the session cookie or an API key, whichever was presented.

    Everything that depends on this — which is every read endpoint — therefore
    accepts a key without changing a single route.
    """
    token = request.cookies.get(SESSION_COOKIE)
    user = await storage.sessions.resolve(token) if token else None
    if user is None:
        user = await api_key_user(request, storage)
    if user is not None:
        # So a failure later in the request can be logged against its caller.
        request.state.user_id = user.id
    return user


#: Said by every authenticated endpoint when the *hippo* session is missing.
#: Says whose session explicitly, because a bare 「未登录」 reads like a statement
#: about the *source* account to whoever just configured a credential there.
#: The wording is copy for the client; the log only ever sees the code.
NOT_SIGNED_IN = '需要登录 hippo 会话'

#: Stable identifier for the same condition, for clients and log queries.
SESSION_MISSING = 'auth.session_missing'


async def current_user(user: User | None = Depends(current_user_optional)) -> User:
    """Require a signed-in, enabled user."""
    if user is None:
        raise ApiError(NOT_SIGNED_IN, status=401, code=SESSION_MISSING)
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
    'NOT_SIGNED_IN',
    'SESSION_COOKIE',
    'SESSION_MISSING',
    'client_ip',
    'current_user',
    'current_user_optional',
    'get_storage',
    'get_sync_scheduler',
    'parse_group_ids',
    'parse_int',
    'require_admin',
]
