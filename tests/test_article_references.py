"""Reference lookup is read-only; enqueue accepts only direct, authorized URLs."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hippo import article_references as references
from hippo.api.deps import current_user, get_storage
from hippo.api.errors import install_exception_handlers
from hippo.api.routers import article as article_api
from hippo.article_queries import ARTICLE_ACCESS_JOIN, SUBSCRIPTION_JOIN, _assert_article_visible, _ensure_image_visible
from hippo.exceptions import ApiError
from hippo.wechat_links import parse_wechat_link

LONG = 'https://mp.weixin.qq.com/s?__biz=Mz1&mid=123&idx=2&sn=abc'
SHORT = 'https://mp.weixin.qq.com/s/slug'


def storage():
    return SimpleNamespace(
        references=SimpleNamespace(
            resolve=AsyncMock(return_value={}),
            states=AsyncMock(return_value={}),
            register=AsyncMock(),
            enqueue=AsyncMock(),
        )
    )


class ReferenceServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.storage = storage()
        self.visible = AsyncMock()
        self.source = AsyncMock(
            return_value={
                'followed': True,
                'content_json': [{'type': 'paragraph', 'text': f'**[Title]({LONG})**'}],
            }
        )
        self.patches = [
            patch.object(references, '_assert_article_visible', self.visible),
            patch.object(references, 'fetchone_row', self.source),
        ]
        for active in self.patches:
            active.start()
            self.addCleanup(active.stop)

    async def test_read_only_lookup_never_queues_or_writes(self):
        result = await references.article_references(self.storage, 7, 1)
        self.visible.assert_awaited_once_with(self.storage, 7, 1)
        self.assertEqual('missing', result['items'][0]['status'])
        self.storage.references.register.assert_not_awaited()
        self.storage.references.enqueue.assert_not_awaited()
        self.assertNotIn('FOR SHARE', self.source.await_args.args[1])

    async def test_an_existing_body_is_linked_without_an_upstream_request(self):
        identity = parse_wechat_link(LONG).identity
        self.storage.references.resolve.return_value = {
            identity: {'id': 2, 'title': 'Stored title', 'link': SHORT, 'ready': True},
        }
        result = await references.article_references(self.storage, 7, 1, enqueue=True)
        item = result['items'][0]
        self.assertEqual(
            ('ready', 2, 'Stored title', SHORT),
            (item['status'], item['article_id'], item['title'], item['canonical_url']),
        )
        self.storage.references.enqueue.assert_not_awaited()
        self.storage.references.register.assert_awaited_once()
        self.assertIn('FOR SHARE OF c', self.source.await_args.args[1])

    async def test_a_body_completed_during_enqueue_is_bound_before_publishing_ready(self):
        identity = parse_wechat_link(LONG).identity
        ready = {identity: {'id': 2, 'title': 'Stored title', 'link': SHORT, 'ready': True}}
        self.storage.references.resolve.side_effect = [{}, ready]
        result = await references.article_references(self.storage, 7, 1, enqueue=True)
        self.assertEqual('ready', result['items'][0]['status'])
        self.assertEqual(2, result['items'][0]['article_id'])
        self.storage.references.register.assert_awaited_with(1, [parse_wechat_link(LONG)], ready)

    async def test_metadata_without_a_body_is_queued(self):
        identity = parse_wechat_link(LONG).identity
        self.storage.references.resolve.return_value = {
            identity: {'id': 2, 'title': 'Stored title', 'link': SHORT, 'ready': False},
        }
        self.storage.references.states.return_value = {identity: {'state': 'pending'}}
        result = await references.article_references(self.storage, 7, 1, enqueue=True)
        self.assertEqual('pending', result['items'][0]['status'])
        self.assertIsNone(result['items'][0]['article_id'])
        self.storage.references.enqueue.assert_awaited_once_with(parse_wechat_link(LONG), article_pk=1, retry=False)

    async def test_repeated_spellings_share_one_enqueue(self):
        self.source.return_value['content_json'].append({'type': 'link', 'href': LONG + '&scene=21'})
        result = await references.article_references(self.storage, 7, 1, enqueue=True)
        self.assertEqual(2, len(result['items']))
        self.assertEqual(1, self.storage.references.enqueue.await_count)

    async def test_only_an_explicit_source_url_retries_a_failed_task(self):
        identity = parse_wechat_link(LONG).identity
        self.storage.references.states.return_value = {identity: {'state': 'failed'}}
        result = await references.article_references(self.storage, 7, 1, enqueue=True)
        self.assertEqual('failed', result['items'][0]['status'])
        self.storage.references.enqueue.assert_awaited_with(parse_wechat_link(LONG), article_pk=1, retry=False)
        await references.article_references(self.storage, 7, 1, enqueue=True, retry_url=LONG)
        self.storage.references.enqueue.assert_awaited_with(parse_wechat_link(LONG), article_pk=1, retry=True)

    async def test_a_retry_cannot_be_used_as_an_arbitrary_url_fetcher(self):
        with self.assertRaises(ApiError) as raised:
            await references.article_references(self.storage, 7, 1, enqueue=True, retry_url=SHORT)
        self.assertEqual(400, raised.exception.status)
        self.storage.references.register.assert_not_awaited()
        self.storage.references.enqueue.assert_not_awaited()

    async def test_an_unsubscribed_referenced_article_does_not_expand_the_crawl(self):
        self.source.return_value['followed'] = False
        result = await references.article_references(self.storage, 7, 1, enqueue=True)
        self.assertEqual({'items': []}, result)
        self.storage.references.resolve.assert_not_awaited()
        self.storage.references.enqueue.assert_not_awaited()

    async def test_an_invisible_source_never_reaches_lookup_or_queue(self):
        self.visible.side_effect = ApiError('Article not found', status=404)
        with self.assertRaises(ApiError):
            await references.article_references(self.storage, 7, 1, enqueue=True)
        self.source.assert_not_awaited()
        self.storage.references.enqueue.assert_not_awaited()

    async def test_invalid_or_absent_content_is_an_empty_reference_list(self):
        for content in (None, {}, 'invalid json', '[]', [{'type': 'paragraph', 'text': 'Only a title'}]):
            self.source.return_value['content_json'] = content
            self.assertEqual({'items': []}, await references.article_references(self.storage, 7, 1))

    async def test_excessive_link_counts_are_rejected_without_partial_queueing(self):
        self.source.return_value['content_json'] = [
            {'type': 'link', 'href': f'https://mp.weixin.qq.com/s/slug-{i}'}
            for i in range(references.MAX_REFERENCE_LINKS + 1)
        ]
        with self.assertRaises(ApiError) as raised:
            await references.article_references(self.storage, 7, 1, enqueue=True)
        self.assertEqual(422, raised.exception.status)
        self.storage.references.enqueue.assert_not_awaited()


class ReferenceAccessSqlTest(unittest.IsolatedAsyncioTestCase):
    async def test_detail_and_image_gates_share_the_same_direct_reference_predicate(self):
        with patch('hippo.article_queries.fetchone_row', AsyncMock(return_value={'ok': 1})) as fetch:
            await _assert_article_visible(SimpleNamespace(), 7, 2)
            await _ensure_image_visible(SimpleNamespace(), 7, 9)
        for call in fetch.await_args_list:
            self.assertIn(ARTICLE_ACCESS_JOIN, call.args[1])
            self.assertEqual(7, call.args[2][0])
        self.assertIn('r.target_article_pk = a.id', ARTICLE_ACCESS_JOIN)
        self.assertIn('origin_content.updated_at = r.source_updated_at', ARTICLE_ACCESS_JOIN)
        self.assertIn('s.user_id = viewer.user_id AND NOT s.is_disabled', ARTICLE_ACCESS_JOIN)
        self.assertIn('origin.id = r.source_article_pk', ARTICLE_ACCESS_JOIN)
        self.assertNotIn('article_reference', SUBSCRIPTION_JOIN)

    async def test_unauthorized_reference_and_image_requests_are_generic_404s(self):
        with patch('hippo.article_queries.fetchone_row', AsyncMock(return_value=None)):
            for check in (_assert_article_visible, _ensure_image_visible):
                with self.assertRaises(ApiError) as raised:
                    await check(SimpleNamespace(), 7, 2)
                self.assertEqual(404, raised.exception.status)


class ReferenceHttpTest(unittest.TestCase):
    def test_read_and_queue_routes_publish_only_the_declared_response_fields(self):
        app = FastAPI()
        install_exception_handlers(app)
        app.include_router(article_api.router, prefix='/api')
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=7)
        db = SimpleNamespace(transaction=lambda: _Transaction())
        app.dependency_overrides[get_storage] = lambda: db
        handler = AsyncMock(
            return_value={
                'items': [
                    {
                        'url': LONG,
                        'status': 'ready',
                        'article_id': 2,
                        'title': 'Title',
                        'canonical_url': SHORT,
                        'internal_secret': 'not public',
                    }
                ]
            }
        )
        with patch.object(article_api, 'article_references', handler), TestClient(app) as client:
            read = client.get('/api/article/1/reference')
            queued = client.post('/api/article/1/reference', json={})
            self.assertEqual(200, read.status_code)
            self.assertEqual(202, queued.status_code)
            self.assertNotIn('internal_secret', queued.text)
            self.assertEqual(7, handler.await_args.args[1])
            self.assertTrue(handler.await_args.kwargs['enqueue'])


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False
