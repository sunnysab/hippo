"""Personal ASR correction rule endpoints."""

from typing import Any

from fastapi import APIRouter, Depends, status

from ...article_queries import _get_article
from ...corrections import effective_rules, project_blocks
from ...exceptions import ApiError
from ...models import User
from ...storage import PostgresStorage
from ..deps import current_user, get_storage
from ..schemas.correction import (
    CorrectionInput,
    CorrectionListOut,
    CorrectionOut,
    CorrectionPreviewInput,
    CorrectionPreviewOut,
)

router = APIRouter(dependencies=[Depends(current_user)])


async def _validate(storage: PostgresStorage, user_id: int, body: CorrectionInput, rule_id: int | None) -> None:
    rules = await storage.corrections.list_for_user(user_id)
    if rule_id is not None and not any(rule['id'] == rule_id for rule in rules):
        raise ApiError('Correction rule not found', status=404)
    if rule_id is None and len(rules) >= 1000:
        raise ApiError('Correction rule limit reached', status=400)
    if not await storage.corrections.valid_scope(user_id, body.account_biz):
        raise ApiError('Scope must contain only subscribed accounts', status=400)
    scope = set(body.account_biz)
    for rule in rules:
        if rule['id'] == rule_id or rule['source_text'] != body.source_text:
            continue
        existing = set(rule['account_biz'])
        if (not scope and not existing) or (scope and existing and scope.intersection(existing)):
            raise ApiError('Correction rule conflicts with an existing rule', status=409)


@router.get('/correction', response_model=CorrectionListOut)
async def list_corrections(
    user: User = Depends(current_user), storage: PostgresStorage = Depends(get_storage)
) -> dict[str, Any]:
    return {'items': await storage.corrections.list_for_user(user.id)}


@router.post('/correction', status_code=status.HTTP_201_CREATED, response_model=CorrectionOut)
async def create_correction(
    body: CorrectionInput, user: User = Depends(current_user), storage: PostgresStorage = Depends(get_storage)
) -> dict[str, Any]:
    async with storage.transaction():
        await storage.corrections.lock_user(user.id)
        await _validate(storage, user.id, body, None)
        return await storage.corrections.create(user.id, body.model_dump())


@router.post('/correction/preview', response_model=CorrectionPreviewOut)
async def preview_correction(
    body: CorrectionPreviewInput,
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    payload = await _get_article(storage, user.id, body.article_id)
    rules = await storage.corrections.list_for_user(user.id)
    if body.editing_rule_id is not None and not any(rule['id'] == body.editing_rule_id for rule in rules):
        raise ApiError('Correction rule not found', status=404)
    if not await storage.corrections.valid_scope(user.id, body.account_biz):
        raise ApiError('Scope must contain only subscribed accounts', status=400)
    for rule in rules:
        if (
            rule['id'] != body.editing_rule_id
            and rule['source_text'] == body.source_text
            and set(rule['account_biz']) == set(body.account_biz)
        ):
            return {'status': 'conflict', 'match_count': 0, 'contexts': [], 'existing_rule_id': rule['id']}
    await _validate(storage, user.id, body, body.editing_rule_id)
    draft = body.model_dump(exclude={'article_id', 'editing_rule_id'})
    rules = [rule for rule in rules if rule['id'] != body.editing_rule_id] + [draft]
    biz = payload['article']['biz']
    blocks = payload.get('content') or []
    originals = [
        {**block, 'text': block['original_text']} if isinstance(block, dict) and 'original_text' in block else block
        for block in blocks
    ]
    asr = [
        block
        for block in originals
        if isinstance(block, dict)
        and block.get('type') == 'paragraph'
        and block.get('transcript_source') == 'asr'
        and isinstance(block.get('text'), str)
    ]
    if not asr:
        return {'status': 'no_asr', 'match_count': 0, 'contexts': []}
    if draft not in effective_rules(rules, biz):
        return {'status': 'not_applicable', 'match_count': 0, 'contexts': []}
    count = 0
    contexts = []
    for block in project_blocks(asr, rules, biz):
        original = block['original_text']
        corrected = block['text']
        for span in block['correction_spans']:
            raw = original.encode('utf-16-le')
            start, end = span['original_start'] * 2, span['original_end'] * 2
            if raw[start:end].decode('utf-16-le') != body.source_text:
                continue
            count += 1
            if len(contexts) < 3:
                left = len(raw[:start].decode('utf-16-le'))
                right = len(raw[:end].decode('utf-16-le'))
                output = corrected.encode('utf-16-le')
                out_left = len(output[: span['start'] * 2].decode('utf-16-le'))
                out_right = len(output[: span['end'] * 2].decode('utf-16-le'))
                contexts.append(
                    {
                        'before': original[max(0, left - 60) : right + 60],
                        'after': corrected[max(0, out_left - 60) : out_right + 60],
                    }
                )
    return {'status': 'ok', 'match_count': count, 'contexts': contexts}


@router.put('/correction/{rule_id}', response_model=CorrectionOut)
async def update_correction(
    rule_id: int,
    body: CorrectionInput,
    user: User = Depends(current_user),
    storage: PostgresStorage = Depends(get_storage),
) -> dict[str, Any]:
    async with storage.transaction():
        await storage.corrections.lock_user(user.id)
        await _validate(storage, user.id, body, rule_id)
        result = await storage.corrections.update(user.id, rule_id, body.model_dump())
        if result is None:
            raise ApiError('Correction rule not found', status=404)
        return result


@router.delete('/correction/{rule_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_correction(
    rule_id: int, user: User = Depends(current_user), storage: PostgresStorage = Depends(get_storage)
) -> None:
    async with storage.transaction():
        await storage.corrections.lock_user(user.id)
        if not await storage.corrections.delete(user.id, rule_id):
            raise ApiError('Correction rule not found', status=404)
