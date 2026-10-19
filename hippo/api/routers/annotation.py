"""Highlight endpoints.

Scoped to a user twice over: the article must belong to the caller's
subscriptions, and the highlight's own ``user_id`` is part of every delete.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status

from ...article_queries import _get_article
from ...exceptions import ApiError
from ...models import User
from ...repositories.annotation import CONTEXT_LENGTH
from ...storage import PostgresStorage
from ..deps import current_user, get_storage
from ..schemas.article import AnnotationCreateRequest, AnnotationListOut, AnnotationOut

router = APIRouter(dependencies=[Depends(current_user)])

#: A highlight is a sentence or two, not a chapter. The cap keeps a runaway
#: selection from being stored and re-anchored on every open.
MAX_QUOTE_LENGTH = 2000


async def _ensure_article(storage: PostgresStorage, user_id: int, article_id: int) -> None:
    """404 unless the article is visible to this user."""
    await _get_article(storage, user_id, article_id)


@router.get('/article/{article_id}/annotation', response_model=AnnotationListOut)
async def list_annotations(
    article_id: int,
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Every highlight this user made on this article, fetched in one call."""
    await _ensure_article(storage, user.id, article_id)
    return {'items': await storage.annotations.list_for_article(user.id, article_id)}


@router.post('/article/{article_id}/annotation', status_code=status.HTTP_201_CREATED, response_model=AnnotationOut)
async def create_annotation(
    article_id: int,
    body: AnnotationCreateRequest,
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Store a highlight anchored by quote plus surrounding context."""
    await _ensure_article(storage, user.id, article_id)

    if not body.quote.strip():
        raise ApiError('quote 不能为空', status=400)
    if len(body.quote) > MAX_QUOTE_LENGTH:
        raise ApiError(f'选区过长（上限 {MAX_QUOTE_LENGTH} 字）', status=400)

    async with storage.transaction():
        return await storage.annotations.create(
            user_id=user.id,
            article_pk=article_id,
            quote=body.quote,
            prefix=(body.prefix or '')[:CONTEXT_LENGTH],
            suffix=(body.suffix or '')[:CONTEXT_LENGTH],
            note=body.note or '',
            # The colour is a closed set on the request model, so a bad value
            # never reaches the row.
            color=body.color,
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
