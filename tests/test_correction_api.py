from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from hippo.api.routers.correction import create_correction, delete_correction, preview_correction, update_correction
from hippo.api.schemas.correction import CorrectionInput, CorrectionPreviewInput
from hippo.exceptions import ApiError


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Transaction:
    def __init__(self, storage):
        self.storage = storage

    async def __aenter__(self):
        self.storage.in_transaction = True

    async def __aexit__(self, *args):
        self.storage.in_transaction = False


def storage(rules=(), valid=True, deleted=True):
    result = SimpleNamespace(in_transaction=False)

    async def lock(user_id):
        assert result.in_transaction
        assert user_id == 7

    async def check_scope(user_id, scope):
        assert result.in_transaction
        assert user_id == 7
        return valid

    result.corrections = SimpleNamespace(
        lock_user=AsyncMock(side_effect=lock),
        valid_scope=AsyncMock(side_effect=check_scope),
        list_for_user=AsyncMock(return_value=list(rules)),
        create=AsyncMock(return_value={'id': 1}),
        update=AsyncMock(return_value={'id': 1}),
        delete=AsyncMock(return_value=deleted),
    )
    result.transaction = lambda: Transaction(result)
    return result


def body(scope=None):
    return CorrectionInput(source_text='a', replacement_text='b', account_biz=scope or [], enabled=True)


@pytest.mark.anyio
async def test_create_scope_conflicts_and_transaction():
    user = SimpleNamespace(id=7)
    existing = {'id': 1, 'source_text': 'a', 'account_biz': ['one']}
    db = storage([existing])
    with pytest.raises(ApiError) as exc:
        await create_correction(body(['one']), user, db)
    assert exc.value.status == 409
    db.corrections.create.assert_not_awaited()
    await create_correction(body(), user, db)
    db.corrections.list_for_user.assert_awaited_with(7)
    db.corrections.create.assert_awaited_once()
    invalid = storage(valid=False)
    with pytest.raises(ApiError) as exc:
        await create_correction(body(['other-users-account']), user, invalid)
    assert exc.value.status == 400


@pytest.mark.anyio
async def test_foreign_update_and_delete_are_404():
    user = SimpleNamespace(id=7)
    db = storage(deleted=False)
    with pytest.raises(ApiError) as exc:
        await update_correction(44, body(), user, db)
    assert exc.value.status == 404
    with pytest.raises(ApiError) as exc:
        await delete_correction(44, user, db)
    assert exc.value.status == 404
    db.corrections.delete.assert_awaited_once_with(7, 44)


@pytest.mark.anyio
async def test_preview_uses_original_and_respects_scope_and_visibility(monkeypatch):
    user = SimpleNamespace(id=7)
    blocks = [
        {'type': 'paragraph', 'transcript_source': 'asr', 'text': 'a a'},
        {'type': 'paragraph', 'transcript_source': 'subtitle', 'text': 'a'},
    ]
    article = AsyncMock(return_value={'article': {'biz': 'one'}, 'content': blocks})
    monkeypatch.setattr('hippo.api.routers.correction._get_article', article)
    db = storage()
    db.corrections.valid_scope = AsyncMock(return_value=True)
    draft = CorrectionPreviewInput(article_id=5, source_text='a', replacement_text='b', account_biz=[], enabled=True)
    result = await preview_correction(draft, user, db)
    assert result['match_count'] == 2
    assert result['contexts'][0] == {'before': 'a a', 'after': 'b b'}
    assert blocks[0]['text'] == 'a a'
    draft.account_biz = ['other']
    assert (await preview_correction(draft, user, db))['status'] == 'not_applicable'
    article.side_effect = ApiError('Article not found', status=404)
    with pytest.raises(ApiError) as exc:
        await preview_correction(draft, user, db)
    assert exc.value.status == 404


@pytest.mark.anyio
async def test_preview_exact_conflict_reports_own_rule_and_no_asr(monkeypatch):
    monkeypatch.setattr(
        'hippo.api.routers.correction._get_article',
        AsyncMock(return_value={'article': {'biz': 'one'}, 'content': [{'type': 'paragraph', 'text': 'a'}]}),
    )
    existing = {'id': 3, 'source_text': 'a', 'replacement_text': 'c', 'account_biz': [], 'enabled': True}
    db = storage([existing])
    db.corrections.valid_scope = AsyncMock(return_value=True)
    draft = CorrectionPreviewInput(article_id=5, source_text='a', replacement_text='b', account_biz=[], enabled=True)
    result = await preview_correction(draft, SimpleNamespace(id=7), db)
    assert result['status'] == 'conflict' and result['existing_rule_id'] == 3
    draft.editing_rule_id = 3
    assert (await preview_correction(draft, SimpleNamespace(id=7), db))['status'] == 'no_asr'
    db.corrections.create.assert_not_awaited()


@pytest.mark.anyio
async def test_preview_protects_tokens_and_rejects_disabled_scope(monkeypatch):
    blocks = [
        {'type': 'paragraph', 'transcript_source': 'asr', 'text': 'wrong https://x/wrong `wrong` 12:34 **wrong**'}
    ]
    monkeypatch.setattr(
        'hippo.api.routers.correction._get_article',
        AsyncMock(return_value={'article': {'biz': 'one'}, 'content': blocks}),
    )
    db = storage()
    db.corrections.valid_scope = AsyncMock(return_value=True)
    draft = CorrectionPreviewInput(
        article_id=5,
        source_text='wrong',
        replacement_text='right',
        account_biz=['one'],
        enabled=True,
    )
    result = await preview_correction(draft, SimpleNamespace(id=7), db)
    assert result['match_count'] == 2
    assert result['contexts'][0]['after'] == 'right https://x/wrong `wrong` 12:34 **right**'
    db.corrections.valid_scope.return_value = False
    with pytest.raises(ApiError) as exc:
        await preview_correction(draft, SimpleNamespace(id=7), db)
    assert exc.value.status == 400
    db.corrections.create.assert_not_awaited()


@pytest.mark.anyio
async def test_preview_protects_nested_labels_but_corrects_text_after_url(monkeypatch):
    original = '[说明 [错词]](https://example.com) https://example.com，错词'
    monkeypatch.setattr(
        'hippo.api.routers.correction._get_article',
        AsyncMock(
            return_value={
                'article': {'biz': 'one'},
                'content': [{'type': 'paragraph', 'transcript_source': 'asr', 'text': original}],
            }
        ),
    )
    db = storage()
    db.corrections.valid_scope = AsyncMock(return_value=True)
    draft = CorrectionPreviewInput(
        article_id=5, source_text='错词', replacement_text='正词', account_biz=[], enabled=True
    )
    result = await preview_correction(draft, SimpleNamespace(id=7), db)
    assert result['match_count'] == 1
    assert result['contexts'][0]['after'].endswith('https://example.com，正词')
    assert '[说明 [错词]]' in result['contexts'][0]['after']


def test_validation_and_scope_normalization():
    assert body(['z', 'a', 'z']).account_biz == ['a', 'z']
    for values in [
        {'source_text': '  '},
        {'replacement_text': ''},
        {'replacement_text': 'a'},
        {'source_text': 'x' * 201},
        {'enabled': 1},
        {'account_biz': [str(i) for i in range(101)]},
    ]:
        data = dict(source_text='a', replacement_text='b', account_biz=[], enabled=True)
        data.update(values)
        with pytest.raises(ValidationError):
            CorrectionInput(**data)
