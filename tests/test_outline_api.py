"""Article authorization, cache invalidation and queue HTTP semantics."""

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hippo.api import deps
from hippo.api.errors import install_exception_handlers
from hippo.api.routers import outline as api
from hippo.exceptions import ApiError
from hippo.outline import snapshot

NOW = datetime(2026, 10, 19, tzinfo=UTC)
SOURCE = {'title': 'Article', 'content_json': [{'type': 'paragraph', 'text': 'Body'}], 'content_updated_at': NOW}


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def cached(state='ready', fingerprint=None):
    return {
        'state': state,
        'fingerprint': fingerprint or snapshot(SOURCE['title'], SOURCE['content_json']).fingerprint,
        'sections': [{'title': 'Topic', 'start_block': 0, 'children': []}],
        'updated_at': NOW,
        'model': 'test-model',
        'error_code': None,
    }


class OutlineApiTest(unittest.TestCase):
    def setUp(self):
        self.storage = SimpleNamespace(
            outlines=SimpleNamespace(
                source=AsyncMock(return_value=SOURCE),
                get=AsyncMock(return_value=None),
                enqueue=AsyncMock(return_value=cached('queued')),
            ),
            transaction=Transaction,
        )
        app = FastAPI()
        install_exception_handlers(app)
        app.include_router(api.router, prefix='/api')
        app.dependency_overrides[deps.get_storage] = lambda: self.storage
        app.dependency_overrides[deps.current_user] = lambda: SimpleNamespace(id=7)
        self.client = TestClient(app)
        self.visible = patch.object(api, '_assert_article_visible', AsyncMock()).start()
        self.provider = patch.object(api, 'resolve_provider', AsyncMock(return_value={'model': 'test'})).start()
        self.addCleanup(patch.stopall)

    def test_missing_outline_is_not_an_error_or_an_automatic_llm_call(self):
        response = self.client.get('/api/article/42/outline')
        self.assertEqual(200, response.status_code)
        self.assertEqual('missing', response.json()['state'])
        self.assertTrue(response.json()['can_generate'])
        self.visible.assert_awaited_once_with(self.storage, 7, 42)
        self.provider.assert_not_awaited()

    def test_invisible_articles_are_rejected_before_cache_or_source_reads(self):
        self.visible.side_effect = ApiError('Article not found', status=404)
        for method in ('get', 'post'):
            self.assertEqual(404, getattr(self.client, method)('/api/article/42/outline').status_code)
        self.storage.outlines.source.assert_not_awaited()
        self.storage.outlines.get.assert_not_awaited()
        self.storage.outlines.enqueue.assert_not_awaited()

    def test_stale_results_never_publish_links(self):
        self.storage.outlines.get.return_value = cached(fingerprint='old')
        body = self.client.get('/api/article/42/outline').json()
        self.assertEqual('stale', body['state'])
        self.assertEqual([], body['sections'])

    def test_ready_cache_does_not_require_a_provider(self):
        self.storage.outlines.get.return_value = cached()
        response = self.client.post('/api/article/42/outline', json={})
        self.assertEqual(200, response.status_code)
        self.assertEqual('ready', response.json()['state'])
        self.provider.assert_not_awaited()
        self.storage.outlines.enqueue.assert_not_awaited()

    def test_generation_is_queued_and_returns_202(self):
        response = self.client.post('/api/article/42/outline', json={'content_updated_at': NOW.isoformat()})
        self.assertEqual(202, response.status_code)
        self.assertEqual('queued', response.json()['state'])
        self.storage.outlines.enqueue.assert_awaited_once()

    def test_force_does_not_restart_an_active_generation(self):
        self.storage.outlines.get.return_value = cached('processing')
        self.assertEqual(202, self.client.post('/api/article/42/outline', json={'force': True}).status_code)
        self.storage.outlines.enqueue.assert_not_awaited()

    def test_force_regenerates_a_ready_result(self):
        self.storage.outlines.get.return_value = cached()
        self.assertEqual(202, self.client.post('/api/article/42/outline', json={'force': True}).status_code)
        self.assertTrue(self.storage.outlines.enqueue.await_args.kwargs['force'])

    def test_empty_body_is_not_mistaken_for_an_archived_playable_video(self):
        self.storage.outlines.source.return_value = dict(
            SOURCE, content_json=[{'type': 'video', 'url': '/api/media/42/video'}]
        )
        response = self.client.post('/api/article/42/outline', json={})
        self.assertEqual(409, response.status_code)
        self.assertEqual('outline.no_text', response.json()['code'])
        self.storage.outlines.enqueue.assert_not_awaited()

    def test_old_reader_snapshot_is_rejected(self):
        response = self.client.post('/api/article/42/outline', json={'content_updated_at': '2020-01-01T00:00:00Z'})
        self.assertEqual(409, response.status_code)
        self.assertEqual('outline.content_changed', response.json()['code'])
        self.storage.outlines.enqueue.assert_not_awaited()

    def test_missing_provider_fails_before_enqueue(self):
        self.provider.side_effect = ApiError('No provider configured.', status=503)
        self.assertEqual(503, self.client.post('/api/article/42/outline', json={}).status_code)
        self.storage.outlines.enqueue.assert_not_awaited()
