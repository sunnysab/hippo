"""The documented response shapes, checked against the rows the routes produce.

A response model is an allowlist, so the failure it can cause is a field
disappearing from a response a caller already used. These tests drive the real
routes with a representative row - the columns the queries actually select - and
assert that every one of them survives, and that the document names the same
shape the runtime returns.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hippo.api import deps
from hippo.api.errors import install_exception_handlers
from hippo.api.routers import account as account_api
from hippo.api.routers import annotation as annotation_api
from hippo.api.routers import article as article_api
from hippo.api.routers import daemon as daemon_api
from hippo.api.routers import feed as feed_api
from hippo.api.routers import source as source_api
from hippo.models import AccountGroup

#: A row of ``accounts`` joined with ``subscription``, as both account queries
#: select it. ``last_synced_at`` is the driver's datetime, not a string.
ACCOUNT_ROW: dict[str, Any] = {
    'biz': 'Mz123',
    'nickname': '中投数研',
    'source': 'weixin',
    'alias': '',
    'round_head_img': None,
    'group_id': 1,
    'is_disabled': False,
    'last_synced_at': datetime(2026, 1, 1, tzinfo=UTC),
    'sync_mode': None,
    'sync_recent_days': None,
    'sync_interval_days': None,
    'group_name': '默认',
    'article_count': 3,
    'avatar_ready': False,
    'backfill_pending': False,
    'backfill_running': False,
    'avatar_url': '/api/account/Mz123/avatar',
}

#: A row of the article query, including the fields the list computes and the
#: relevance score a search adds.
ARTICLE_ROW: dict[str, Any] = {
    'id': 9,
    'biz': 'Mz123',
    'article_id': 'slug-1',
    'title': '标题',
    'item_show_type': 0,
    'author': '作者',
    'digest': '摘要',
    'cover': 5,
    'link': 'https://mp.weixin.qq.com/s/AbCdEf',
    'source_url': None,
    'publish_at': 1700000000,
    'duration_seconds': None,
    'created_at': '2026-01-01T00:00:00+00:00',
    'read_at': None,
    'starred_at': None,
    'source': 'weixin',
    'account_nickname': '中投数研',
    'account_alias': '',
    'account_avatar': None,
    'group_id': 1,
    'group_name': '默认',
    'image_id': 5,
    'content_kind': 'article',
    'account_avatar_url': '/api/account/Mz123/avatar',
    'rank': 0.25,
}

IMAGE_ROW: dict[str, Any] = {
    'id': 5,
    'position': 0,
    'kind': 'inline',
    'content_type': 'image/jpeg',
    'hash_algo': 'sha256',
    'content_hash': 'abc123',
}

ANNOTATION_ROW: dict[str, Any] = {
    'id': 3,
    'article_id': 9,
    'quote': '一句',
    'prefix': '',
    'suffix': '',
    'note': '',
    'color': 'default',
    'created_at': '2026-01-01T00:00:00+00:00',
}


class _NullTransaction:
    async def __aenter__(self) -> _NullTransaction:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _client(router: Any, storage: SimpleNamespace, *, optional_user: bool = False) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(router.router, prefix='/api')
    app.dependency_overrides[deps.get_storage] = lambda: storage
    user = SimpleNamespace(id=1, username='alice', role='user')
    app.dependency_overrides[deps.current_user] = lambda: user
    if optional_user:
        # The feed resolves its own credential, so it depends on the optional
        # one and the strict override above never runs for it.
        app.dependency_overrides[deps.current_user_optional] = lambda: user
    return TestClient(app)


def _expected(value: Any) -> Any:
    """Rows carry datetimes; a JSON response carries their ISO form."""
    return value.isoformat().replace('+00:00', 'Z') if isinstance(value, datetime) else value


def _assert_row_survives(test: unittest.TestCase, row: dict[str, Any], payload: dict[str, Any]) -> None:
    for key, value in row.items():
        test.assertIn(key, payload, f'{key} was dropped from the response')
        test.assertEqual(_expected(value), payload[key], key)


class AccountResponseTest(unittest.TestCase):
    def test_a_list_row_reaches_the_caller_whole(self) -> None:
        payload = {'accounts': [dict(ACCOUNT_ROW)], 'page': 1, 'page_size': 20, 'total': 1}
        storage = SimpleNamespace()

        with patch.object(account_api, '_list_accounts', AsyncMock(return_value=payload)):
            body = _client(account_api, storage).get('/api/account').json()

        _assert_row_survives(self, ACCOUNT_ROW, body['accounts'][0])
        self.assertEqual({'accounts', 'page', 'page_size', 'total'}, set(body))

    def test_a_detail_row_reaches_the_caller_whole(self) -> None:
        with patch.object(account_api, '_get_account', AsyncMock(return_value=dict(ACCOUNT_ROW))):
            body = _client(account_api, SimpleNamespace()).get('/api/account/Mz123').json()

        _assert_row_survives(self, ACCOUNT_ROW, body)

    def test_the_group_list_carries_the_default_group(self) -> None:
        group = AccountGroup(id=1, name='默认', account_count=2)
        storage = SimpleNamespace(groups=SimpleNamespace(list_groups=AsyncMock(return_value=[group])))

        with patch.object(account_api, 'ensure_default_group', AsyncMock(return_value=SimpleNamespace(id=1))):
            body = _client(account_api, storage).get('/api/group').json()

        self.assertEqual(1, body['default_group_id'])
        self.assertEqual(group.model_dump(), body['groups'][0])


class ArticleResponseTest(unittest.TestCase):
    def test_a_list_row_keeps_its_search_score_and_computed_fields(self) -> None:
        payload = {
            'articles': [dict(ARTICLE_ROW)],
            'page': 1,
            'page_size': 20,
            'total': 1,
            'item_show_type_facets': [{'item_show_type': 0, 'count': 1}],
        }
        storage = SimpleNamespace(users=SimpleNamespace(get_preferences=AsyncMock(return_value={})))

        with patch.object(article_api, '_list_articles', AsyncMock(return_value=payload)):
            body = _client(article_api, storage).get('/api/article').json()

        _assert_row_survives(self, ARTICLE_ROW, body['articles'][0])
        self.assertEqual([{'item_show_type': 0, 'count': 1}], body['item_show_type_facets'])

    def test_a_detail_body_keeps_its_blocks_and_images(self) -> None:
        detail = {
            'article': dict(ARTICLE_ROW),
            'content': [{'type': 'paragraph', 'text': '正文', 't': 1.5}],
            'content_status': 'ok',
            'content_updated_at': None,
            'content_fetch_diagnostic': None,
            'images': [dict(IMAGE_ROW)],
        }

        with patch.object(article_api, '_get_article', AsyncMock(return_value=detail)):
            body = _client(article_api, SimpleNamespace()).get('/api/article/9').json()

        _assert_row_survives(self, ARTICLE_ROW, body['article'])
        _assert_row_survives(self, IMAGE_ROW, body['images'][0])
        self.assertEqual('正文', body['content'][0]['text'])
        self.assertEqual(1.5, body['content'][0]['t'])
        # A field the block does not carry is not invented in the response.
        self.assertNotIn('video_id', body['content'][0])

    def test_a_block_keeps_a_provider_key_the_reader_may_use(self) -> None:
        # A block is written by the source, so the reader's copy stays faithful:
        # the model declares the reader's fields and lets anything else through.
        block = {'type': 'video', 'url': '/api/media/9/video', 'cover_url': 'https://cdn.example/c.jpg', 'x_future': 1}
        detail = {
            'article': dict(ARTICLE_ROW),
            'content': [block],
            'content_status': 'ok',
            'content_updated_at': None,
            'content_fetch_diagnostic': None,
            'images': [],
        }

        with patch.object(article_api, '_get_article', AsyncMock(return_value=detail)):
            body = _client(article_api, SimpleNamespace()).get('/api/article/9').json()

        self.assertEqual(block, body['content'][0])

    def test_the_image_list_matches_its_row(self) -> None:
        with patch.object(article_api, '_list_article_images', AsyncMock(return_value=[dict(IMAGE_ROW)])):
            body = _client(article_api, SimpleNamespace()).get('/api/article/9/image').json()

        _assert_row_survives(self, IMAGE_ROW, body['images'][0])

    def test_the_feed_row_is_the_stored_columns_only(self) -> None:
        # The feed runs the same query without the article list's computed
        # fields; its model must not demand them.
        row = {k: v for k, v in ARTICLE_ROW.items() if k not in {'content_kind', 'account_avatar_url'}}
        storage = SimpleNamespace()

        with patch.object(feed_api, '_list_feed', AsyncMock(return_value=[row])):
            body = _client(feed_api, storage, optional_user=True).get('/api/feed/mixed').json()

        _assert_row_survives(self, row, body['articles'][0])


class AnnotationResponseTest(unittest.TestCase):
    def test_a_stored_highlight_comes_back_as_it_is(self) -> None:
        storage = SimpleNamespace(
            annotations=SimpleNamespace(
                list_for_article=AsyncMock(return_value=[dict(ANNOTATION_ROW)]),
                create=AsyncMock(return_value=dict(ANNOTATION_ROW)),
            ),
            transaction=_NullTransaction,
        )

        with patch.object(annotation_api, '_ensure_article', AsyncMock()):
            client = _client(annotation_api, storage)
            listed = client.get('/api/article/9/annotation').json()
            created = client.post('/api/article/9/annotation', json={'quote': '一句'}).json()

        _assert_row_survives(self, ANNOTATION_ROW, listed['items'][0])
        _assert_row_survives(self, ANNOTATION_ROW, created)


class SourceResponseTest(unittest.TestCase):
    def test_the_credential_list_reports_only_configured(self) -> None:
        storage = SimpleNamespace(credentials=SimpleNamespace(configured=AsyncMock(return_value={'bilibili': True})))

        body = _client(source_api, storage).get('/api/source/credential').json()

        self.assertEqual({'bilibili': {'configured': True}, 'youtube': {'configured': False}}, body)

    def test_the_source_rail_is_a_count_per_source(self) -> None:
        storage = SimpleNamespace(
            accounts=SimpleNamespace(count_by_source=AsyncMock(return_value=[{'source': 'weixin', 'channels': 2}]))
        )

        body = _client(source_api, storage).get('/api/source').json()

        self.assertEqual([{'source': 'weixin', 'channels': 2}], body['sources'])

    def test_the_daemon_login_status_fits_its_model_either_way(self) -> None:
        # Reachable and unreachable are two literal payloads; both are the
        # documented shape, so neither can turn into a 500 on the way out.
        live = {
            'logged_in': True,
            'status': 'online',
            'need_relogin': False,
            'wxid': 'wxid_1',
            'nickname': '夜行',
            'head_url': None,
            'clients_connected': 1,
        }

        with patch.object(daemon_api, 'WeixinSource', lambda **_kwargs: _FakeDaemon(status=live)):
            body = _client(daemon_api, SimpleNamespace()).get('/api/login').json()

        self.assertEqual({**live, 'error': ''}, body)

        with patch.object(daemon_api, 'WeixinSource', lambda **_kwargs: _FakeDaemon(error=RuntimeError('boom'))):
            offline = _client(daemon_api, SimpleNamespace()).get('/api/login').json()

        self.assertFalse(offline['logged_in'])
        self.assertEqual('unreachable', offline['status'])
        self.assertEqual('boom', offline['error'])


class _FakeDaemon:
    def __init__(self, *, status: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self._status = status
        self._error = error

    async def __aenter__(self) -> _FakeDaemon:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def status(self) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return self._status or {}


if __name__ == '__main__':
    unittest.main()
