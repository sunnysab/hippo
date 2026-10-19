"""Queue processing releases connections, persists only complete/current results."""

import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo import outline_worker as worker
from hippo.exceptions import ApiError
from hippo.outline import snapshot


class WorkerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = {'title': 'Title', 'content_json': [{'type': 'paragraph', 'text': 'Body'}]}
        self.claim = {
            'article_pk': 1,
            'fingerprint': snapshot('Title', self.source['content_json']).fingerprint,
            'lease_token': 'token',
        }
        self.repo = SimpleNamespace(
            claim=AsyncMock(return_value=self.claim),
            source=AsyncMock(return_value=self.source),
            finish=AsyncMock(),
            release=AsyncMock(),
        )
        self.connections = 0

        @asynccontextmanager
        async def transaction():
            yield

        @asynccontextmanager
        async def storage():
            self.connections += 1
            try:
                yield SimpleNamespace(outlines=self.repo, transaction=transaction)
            finally:
                self.connections -= 1

        self.patches = [
            patch.object(worker, 'open_storage', storage),
            patch.object(worker, 'resolve_provider', AsyncMock(return_value={'model': 'test'})),
            patch.object(
                worker,
                'generate_outline',
                AsyncMock(return_value=[{'title': 'Topic', 'start_block': 0, 'children': []}]),
            ),
        ]
        self.generate = None
        for item in self.patches:
            value = item.start()
            if isinstance(value, AsyncMock):
                self.generate = value
        self.addCleanup(patch.stopall)

    async def test_empty_queue_does_nothing(self):
        self.repo.claim.return_value = None
        self.assertFalse(await worker.run_once())
        self.generate.assert_not_awaited()

    async def test_model_runs_without_holding_a_connection(self):
        async def generate(*args):
            self.assertEqual(0, self.connections)
            return [{'title': 'Topic', 'start_block': 0, 'children': []}]

        self.generate.side_effect = generate
        self.assertTrue(await worker.run_once())
        self.assertEqual('test', self.repo.finish.await_args.kwargs['model'])
        self.assertNotIn('error', self.repo.finish.await_args.kwargs)

    async def test_a_model_failure_never_publishes_partial_chapters(self):
        self.generate.side_effect = ApiError('Invalid JSON', code='outline.invalid_result')
        await worker.run_once()
        self.repo.finish.assert_awaited_once_with(self.claim, error='outline.invalid_result')

    async def test_unknown_errors_do_not_leak_exception_text(self):
        self.generate.side_effect = RuntimeError('secret from upstream')
        await worker.run_once()
        self.repo.finish.assert_awaited_once_with(self.claim, error='outline.generation_failed')

    async def test_content_changes_before_and_during_generation(self):
        changed = {'title': 'Other title', 'content_json': self.source['content_json']}
        for rows in ([changed], [self.source, changed]):
            self.repo.source.side_effect = list(rows)
            self.repo.finish.reset_mock()
            await worker.run_once()
            self.repo.finish.assert_awaited_once_with(self.claim, error='outline.content_changed')

    async def test_shutdown_requeues_the_owned_lease(self):
        self.generate.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await worker.run_once()
        self.repo.release.assert_awaited_once_with(self.claim)
        self.repo.finish.assert_not_awaited()

    async def test_total_timeout_is_a_safe_failure(self):
        async def slow(*args):
            await asyncio.sleep(10)

        self.generate.side_effect = slow
        with patch.object(worker, 'JOB_TIMEOUT', 0.01):
            await worker.run_once()
        self.repo.finish.assert_awaited_once_with(self.claim, error='outline.generation_failed')
