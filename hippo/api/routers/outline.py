"""Navigation outlines: authorize the article before inspecting its shared cache."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Response

from ...article_queries import _assert_article_visible
from ...exceptions import ApiError
from ...llm.client import resolve_provider
from ...models import User
from ...outline import snapshot, validate_size
from ...storage import PostgresStorage
from ..deps import current_user, get_storage
from ..schemas.outline import OutlineOut, OutlineRequest

router = APIRouter(dependencies=[Depends(current_user)])


async def _source(storage, user_id: int, article_id: int):
    await _assert_article_visible(storage, user_id, article_id)
    row = await storage.outlines.source(article_id)
    if row is None:
        raise ApiError('Article not found', status=404)
    return row, snapshot(row['title'], row['content_json'])


def _response(row, source, cached) -> dict:
    state = cached['state'] if cached else 'missing'
    if cached and cached['fingerprint'] != source.fingerprint:
        state = 'stale'
    return {
        'state': state,
        'content_updated_at': row['content_updated_at'],
        'sections': cached['sections'] if state == 'ready' else [],
        'model': cached['model'] if cached else None,
        'updated_at': cached['updated_at'] if cached else None,
        'error_code': cached['error_code'] if state == 'failed' else None,
        'can_generate': bool(source.blocks),
    }


@router.get('/article/{article_id}/outline', response_model=OutlineOut)
async def get_outline(
    article_id: int,
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
):
    row, source = await _source(storage, user.id, article_id)
    return _response(row, source, await storage.outlines.get(article_id))


@router.post(
    '/article/{article_id}/outline',
    response_model=OutlineOut,
    responses={202: {'model': OutlineOut, 'description': 'Generation queued or already running.'}},
)
async def request_outline(
    article_id: int,
    response: Response,
    body: OutlineRequest = OutlineRequest(),
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
):
    row, source = await _source(storage, user.id, article_id)
    validate_size(source)
    version = row['content_updated_at']
    if isinstance(version, str):
        version = datetime.fromisoformat(version)
    if body.content_updated_at is not None and body.content_updated_at != version:
        raise ApiError(
            'Reload the updated article before generating an outline.', status=409, code='outline.content_changed'
        )
    cached = await storage.outlines.get(article_id)
    if (
        cached
        and cached['fingerprint'] == source.fingerprint
        and (cached['state'] in ('queued', 'processing') or (cached['state'] == 'ready' and not body.force))
    ):
        response.status_code = 200 if cached['state'] == 'ready' else 202
        return _response(row, source, cached)
    await resolve_provider(storage)
    async with storage.transaction():
        cached = await storage.outlines.enqueue(article_id, source.fingerprint, force=body.force)
    response.status_code = 202 if cached['state'] in ('queued', 'processing') else 200
    return _response(row, source, cached)
