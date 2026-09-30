"""Highlight endpoints.

Scoped to a user twice over: the article must belong to the caller's
subscriptions, and the highlight's own ``user_id`` is part of every delete.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, status

from ...article_queries import _get_article
from ...exceptions import ApiError
from ...models import User
from ...repositories.annotation import CONTEXT_LENGTH
from ...storage import PostgresStorage
from ..deps import current_user, get_storage

router = APIRouter(dependencies=[Depends(current_user)])

#: A highlight is a sentence or two, not a chapter. The cap keeps a runaway
#: selection from being stored and re-anchored on every open.
MAX_QUOTE_LENGTH = 2000

_COLORS = {'default', 'yellow', 'green', 'blue', 'pink'}


async def _ensure_article(storage: PostgresStorage, user_id: int, article_id: int) -> None:
    """404 unless the article is visible to this user."""
    await _get_article(storage, user_id, article_id)


@router.get('/article/{article_id}/annotation')
async def list_annotations(
    article_id: int,
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Every highlight this user made on this article, fetched in one call."""
    await _ensure_article(storage, user.id, article_id)
    return {'items': await storage.annotations.list_for_article(user.id, article_id)}


@router.post('/article/{article_id}/annotation', status_code=status.HTTP_201_CREATED)
async def create_annotation(
    article_id: int,
    body: dict[str, Any] = Body(default={}),
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Store a highlight anchored by quote plus surrounding context."""
    await _ensure_article(storage, user.id, article_id)

    quote = str(body.get('quote') or '')
    if not quote.strip():
        raise ApiError('quote 不能为空', status=400)
    if len(quote) > MAX_QUOTE_LENGTH:
        raise ApiError(f'选区过长（上限 {MAX_QUOTE_LENGTH} 字）', status=400)

    color = str(body.get('color') or 'default')
    if color not in _COLORS:
        raise ApiError(f'未知颜色: {color}', status=400)

    async with storage.transaction():
        return await storage.annotations.create(
            user_id=user.id,
            article_pk=article_id,
            quote=quote,
            prefix=str(body.get('prefix') or '')[:CONTEXT_LENGTH],
            suffix=str(body.get('suffix') or '')[:CONTEXT_LENGTH],
            note=str(body.get('note') or ''),
            color=color,
        )


@router.delete('/article/{article_id}/annotation/{annotation_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_annotation(
    article_id: int,
    annotation_id: int,
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> None:
    """Delete one highlight. Someone else's id is a 404, not a 403."""
    await _ensure_article(storage, user.id, article_id)
    async with storage.transaction():
        deleted = await storage.annotations.delete(annotation_id, user.id)
    if not deleted:
        raise ApiError('划线不存在', status=404)
