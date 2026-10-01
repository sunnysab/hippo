"""LLM provider administration.

Config lives in the database, not in the environment: an admin must be able to
add a provider without a redeploy. ``HIPPO_LLM_*`` seeds the first row only.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends, Request, status

from ...exceptions import ApiError
from ...logger import get_logger
from ...models import User
from ...storage import PostgresStorage
from ..deps import client_ip, get_storage, require_admin

logger = get_logger(__name__)
router = APIRouter()

#: Reachability probe is a cheap GET, so it gets its own short budget.
_PROBE_TIMEOUT = 10.0


def _provider_id(provider_id: str) -> int:
    try:
        return int(provider_id)
    except (TypeError, ValueError) as exc:
        raise ApiError('Invalid provider id', status=400) from exc


def _normalize_base_url(raw: str) -> str:
    return raw.strip().rstrip('/')


@router.get('/llm/provider')
async def list_providers(
    _: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Every provider, with the API key reduced to its last four characters."""
    return {'items': await storage.llm.list_all()}


@router.post('/llm/provider', status_code=status.HTTP_201_CREATED)
async def create_provider(
    request: Request,
    body: dict[str, Any] = Body(default={}),
    actor: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    name = str(body.get('name') or '').strip()
    base_url = _normalize_base_url(str(body.get('base_url') or ''))
    api_key = str(body.get('api_key') or '')
    model = str(body.get('model') or '').strip()
    if not name or not base_url or not api_key or not model:
        raise ApiError('name、base_url、api_key、model 均为必填', status=400)

    async with storage.transaction():
        provider = await storage.llm.create(
            name=name,
            base_url=base_url,
            api_key=api_key,
            model=model,
            is_default=bool(body.get('is_default')),
            enabled=bool(body.get('enabled', True)),
        )
        await storage.audit.record(
            actor.id,
            'admin.llm_provider_created',
            target=name,
            detail={'base_url': base_url, 'model': model},
            ip=client_ip(request),
        )
    return provider


@router.patch('/llm/provider/{provider_id}')
async def update_provider(
    provider_id: str,
    request: Request,
    body: dict[str, Any] = Body(default={}),
    actor: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Patch a provider. An omitted or empty ``api_key`` keeps the stored one."""
    target_id = _provider_id(provider_id)
    if await storage.llm.get(target_id) is None:
        raise ApiError('provider 不存在', status=404)

    changes: dict[str, Any] = {}
    for column in ('name', 'model', 'enabled', 'is_default'):
        if column in body:
            changes[column] = body[column]
    if 'base_url' in body:
        changes['base_url'] = _normalize_base_url(str(body['base_url']))

    # Blank means "unchanged"; the client never receives the stored key, so it
    # cannot echo it back.
    api_key = str(body.get('api_key') or '').strip() or None

    async with storage.transaction():
        updated = await storage.llm.update(target_id, changes, api_key=api_key)
        await storage.audit.record(
            actor.id,
            'admin.llm_provider_updated',
            target=str(updated['name']) if updated else provider_id,
            detail={'changed': sorted([*changes, *(['api_key'] if api_key else [])])},
            ip=client_ip(request),
        )
    return updated or {}


@router.delete('/llm/provider/{provider_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_id: str,
    request: Request,
    actor: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> None:
    target_id = _provider_id(provider_id)
    provider = await storage.llm.get(target_id)
    if provider is None:
        raise ApiError('provider 不存在', status=404)
    async with storage.transaction():
        await storage.llm.delete(target_id)
        await storage.audit.record(
            actor.id,
            'admin.llm_provider_deleted',
            target=str(provider['name']),
            ip=client_ip(request),
        )


@router.post('/llm/provider/{provider_id}/test')
async def test_provider(
    provider_id: str,
    _: User = Depends(require_admin),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Probe ``GET {base_url}/models`` and report reachability and latency.

    Returns 200 with ``ok: false`` rather than an error status: an unreachable
    provider is a normal answer to this question, not a failed request.
    """
    target_id = _provider_id(provider_id)
    provider = await storage.llm.get_full(target_id)
    if provider is None:
        raise ApiError('provider 不存在', status=404)

    url = f'{_normalize_base_url(provider["base_url"])}/models'
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            response = await client.get(url, headers={'Authorization': f'Bearer {provider["api_key"]}'})
        latency_ms = round((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            return {
                'ok': False,
                'latency_ms': latency_ms,
                'error': f'HTTP {response.status_code}',
            }
        models = _model_ids(response)
        return {
            'ok': True,
            'latency_ms': latency_ms,
            'models': models,
            'model_available': provider['model'] in models if models else None,
        }
    except httpx.TimeoutException:
        return {'ok': False, 'latency_ms': None, 'error': '连接超时'}
    except httpx.HTTPError as exc:
        logger.warning('LLM provider probe failed for %s: %s', provider['name'], exc)
        return {'ok': False, 'latency_ms': None, 'error': f'无法连接: {exc}'}


def _model_ids(response: httpx.Response) -> list[str]:
    try:
        payload = response.json()
    except ValueError:
        return []
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    return [str(item['id']) for item in data if isinstance(item, dict) and 'id' in item]
