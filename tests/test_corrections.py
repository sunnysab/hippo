from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hippo.article_queries import _project_transcript
from hippo.corrections import apply_text, effective_rules, project_blocks


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def rule(source, replacement, scope=None, enabled=True):
    return {'source_text': source, 'replacement_text': replacement, 'account_biz': scope or [], 'enabled': enabled}


@pytest.mark.parametrize(
    'original, expected',
    [
        ('[说明 [错词]](https://example.com) 错词', '[说明 [错词]](https://example.com) 正词'),
        ('![说明 [错词]](https://example.com) 错词', '![说明 [错词]](https://example.com) 正词'),
        ('https://example.com，错词', 'https://example.com，正词'),
        ('https://example.com。错词', 'https://example.com。正词'),
        ('https://example.com！错词', 'https://example.com！正词'),
    ],
)
def test_nested_link_and_chinese_url_boundaries(original, expected):
    assert apply_text(original, [rule('错词', '正词')])[0] == expected


def test_literal_longest_specific_and_no_cascade():
    rules = [rule('a', 'z'), rule('a.', 'a'), rule('a.', 'UP', ['one']), rule('z', 'BAD')]
    assert apply_text('a. a', effective_rules(rules, 'one'))[0] == 'UP z'
    assert apply_text('a. a', effective_rules(rules, 'other'))[0] == 'a z'


def test_protected_tokens_and_utf16_spans():
    text = (
        '😀wrong https://example.com/wrong [wrong](https://x/wrong) `wrong` '
        '01:23:45 **wrong** \\*wrong\n```js\nwrong\n```\nwrong'
    )
    result, spans = apply_text(text, [rule('wrong', 'right'), rule('01:23:45', 'bad')])
    assert result == (
        '😀right https://example.com/wrong [wrong](https://x/wrong) `wrong` '
        '01:23:45 **right** \\*right\n```js\nwrong\n```\nright'
    )
    assert len(spans) == 4
    assert spans[0] == {'original_start': 2, 'original_end': 7, 'start': 2, 'end': 7}


def test_timestamp_next_to_unicode_and_fraction_is_protected():
    text = '时间01:23:45.678结束 01:23'
    assert apply_text(text, [rule('01:23:45.678', 'x'), rule('01:23', 'x')]) == (text, [])


def test_long_match_cannot_cross_protected_token_or_hide_short_match():
    text = 'a`code`b **a** 12:34 a'
    result, spans = apply_text(text, [rule('a`code`b', 'bad'), rule('a', 'X')])
    assert result == 'X`code`b **X** 12:34 X'
    assert len(spans) == 3


def test_utf16_spans_and_asr_only_without_mutation():
    blocks = [
        {'type': 'paragraph', 'transcript_source': 'asr', 'text': '😀ab!'},
        {'type': 'paragraph', 'transcript_source': 'subtitle', 'text': 'ab'},
        {'type': 'paragraph', 'text': 'ab'},
        {'type': 'heading', 'transcript_source': 'asr', 'text': 'ab'},
        {'type': 'paragraph', 'transcript_source': 'asr', 'text': 'untouched'},
    ]
    before = deepcopy(blocks)
    result = project_blocks(blocks, [rule('ab', '😀x')], 'one')
    assert blocks == before
    assert result[0]['text'] == '😀😀x!'
    assert result[0]['original_text'] == '😀ab!'
    assert result[0]['correction_spans'] == [{'original_start': 2, 'original_end': 4, 'start': 2, 'end': 5}]
    assert result[1:4] == blocks[1:4]
    assert result[4]['original_text'] == 'untouched'
    assert result[4]['correction_spans'] == []
    assert project_blocks(blocks, [rule('ab', 'x', enabled=False)], 'one')[0]['text'] == '😀ab!'


@pytest.mark.anyio
async def test_projection_loads_only_the_viewers_rules_and_only_for_asr():
    repo = SimpleNamespace(list_for_user=AsyncMock(side_effect=lambda user_id: [rule('a', str(user_id))]))
    db = SimpleNamespace(corrections=repo)
    blocks = [{'type': 'paragraph', 'transcript_source': 'asr', 'text': 'a'}, {'type': 'paragraph', 'text': 'a'}]
    assert (await _project_transcript(db, 1, 'one', blocks))[0]['text'] == '1'
    assert (await _project_transcript(db, 2, 'one', blocks))[0]['text'] == '2'
    assert blocks[0]['text'] == 'a'
    repo.list_for_user.reset_mock()
    assert await _project_transcript(db, 1, 'one', blocks[1:]) == blocks[1:]
    repo.list_for_user.assert_not_awaited()
    fallback = await _project_transcript(SimpleNamespace(), 1, 'one', blocks)
    assert fallback[0]['original_text'] == 'a' and fallback[0]['correction_spans'] == []


@pytest.mark.anyio
async def test_article_detail_and_chat_use_personal_projection(monkeypatch):
    from hippo import article_queries
    from hippo.api.routers import chat

    blocks = [{'type': 'paragraph', 'transcript_source': 'asr', 'text': 'wrong'}]
    original = deepcopy(blocks)

    async def fetch(storage, sql, params, **kwargs):
        if 'FROM articles a' in sql:
            return {'id': 5, 'biz': 'one', 'article_id': 'x', 'title': 'T'}
        return {'content_json': blocks, 'updated_at': 'version'}

    monkeypatch.setattr(article_queries, 'fetchone_row', fetch)
    monkeypatch.setattr(article_queries, '_get_visible_article_images', AsyncMock(return_value=([], set())))
    repo = SimpleNamespace(list_for_user=AsyncMock(side_effect=lambda uid: [rule('wrong', f'right{uid}')]))
    db = SimpleNamespace(corrections=repo)
    detail = await article_queries._get_article(db, 1, 5)
    assert detail['content'][0]['text'] == 'right1'
    assert detail['content_updated_at'] == 'version'
    context, _ = await chat._article_context(db, 2, {'article_id': 5})
    assert 'right2' in context and 'right1' not in context
    assert blocks == original
