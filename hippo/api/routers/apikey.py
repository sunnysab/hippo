"""API keys for the public read API.

A key is the way to read this instance from something that cannot hold a cookie —
a script, a feed reader, another service. It is deliberately not a way to
administer it: see ``API_KEY_FORBIDDEN_PREFIXES``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request, status

from ...exceptions import ApiError
from ...logger import get_logger
from ...models import User
from ...repositories.apikey import SCOPES
from ...storage import PostgresStorage
from ..deps import client_ip, current_user, get_storage
from ..schemas.auth import ApiKeyCreatedOut, ApiKeyCreateRequest, ApiKeyListOut, WhoamiOut

logger = get_logger(__name__)

router = APIRouter()

#: Longest expiry the console may ask for. A key without one is allowed on purpose
#: (this is a personal instance), but a bounded default is friendlier.
MAX_EXPIRY_DAYS = 3650


def _expiry(value: Any) -> datetime | None:
    if value in (None, ''):
        return None
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError('expires_in_days 必须是整数天数', status=400) from exc
    if days <= 0:
        raise ApiError('expires_in_days 必须大于 0', status=400)
    return datetime.now(UTC) + timedelta(days=min(days, MAX_EXPIRY_DAYS))


@router.get('/apikey', response_model=ApiKeyListOut)
async def list_api_keys(
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """This user's keys. The secret is not stored, so it cannot be shown again."""
    return {
        'keys': await storage.api_keys.list_for_user(user.id),
        'scopes': list(SCOPES),
    }


@router.post('/apikey', status_code=status.HTTP_201_CREATED, response_model=ApiKeyCreatedOut)
async def create_api_key(
    request: Request,
    body: ApiKeyCreateRequest,
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Issue a key. The ``secret`` in this response is the only copy that exists."""
    name = (body.name or '').strip() or 'API Key'
    scopes = tuple(scope for scope in (body.scopes or ['read']) if scope in SCOPES) or ('read',)
    async with storage.transaction():
        record, secret = await storage.api_keys.create(
            user.id,
            name=name,
            scopes=scopes,
            expires_at=_expiry(body.expires_in_days),
        )
        await storage.audit.record(
            user.id,
            'apikey.created',
            target=str(record['id']),
            detail={'name': name, 'scopes': list(scopes)},
            ip=client_ip(request),
        )
    logger.info('签发 API Key %s（%s）', record['prefix'], user.username)
    return {**record, 'secret': secret}


@router.delete('/apikey/{key_id}', status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: int,
    request: Request,
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> None:
    """Revoke one of the caller's own keys. Revocation is immediate."""
    async with storage.transaction():
        revoked = await storage.api_keys.revoke(user.id, key_id)
        if not revoked:
            raise ApiError('API Key 不存在或已吊销', status=404)
        await storage.audit.record(user.id, 'apikey.revoked', target=str(key_id), ip=client_ip(request))


@router.get('/whoami', response_model=WhoamiOut)
async def whoami(
    request: Request,
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """What this credential is, so a script can check it before doing work."""
    key = getattr(request.state, 'api_key', None)
    return {
        'via': 'api_key' if key else 'session',
        'user': {'id': user.id, 'username': user.username, 'role': user.role},
        'scopes': list(key['scopes']) if key else list(SCOPES),
        'key': {'id': key['id'], 'name': key['name']} if key else None,
    }


__all__ = ['MAX_EXPIRY_DAYS', 'router']
