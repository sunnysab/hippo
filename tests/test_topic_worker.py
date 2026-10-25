"""The loop backfills, claims one row, and never publishes a partial answer."""

import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo import topic_worker as worker
from hippo.exceptions import ApiError
from hippo.topics import fingerprint

VOCABULARY = {'version': 1, 'topics': [{'slug': 'ai', 'name': '人工智能'}]}
TOPICS = [{'slug': 'ai', 'name': '人工智能'}]


class TopicWorkerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = {'title': 'Title', 'digest': 'Digest'}
        self.claim = {
            'article_pk': 1,
            'fingerprint': '',
            'state': 'processing',
            'topics': [],
            'lease_token': 'token',
        }
        self.repo = SimpleNamespace(
            backfill=AsyncMock(return_value=0),
            requeue_stale=AsyncMock(return_value=0),
            claim=AsyncMock(return_value=self.claim),
            source=AsyncMock(return_value=self.source),
            finish=AsyncMock(return_value=True),
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
                yield SimpleNamespace(
                    topics=self.repo,
                    transaction=transaction,
                )
            finally:
                self.connections -= 1

        self.classify = AsyncMock(return_value=TOPICS)
        self.patches = [
            patch.object(worker, 'open_storage', storage),
            patch.object(worker, '_vocabulary', AsyncMock(return_value=VOCABULARY)),
            patch.object(worker, 'resolve_provider', AsyncMock(return_value={'model': 'test'})),
            patch.object(worker, 'classify', self.classify),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(patch.stopall)

    async def test_an_empty_queue_does_nothing_and_never_calls_the_model(self):
        self.repo.claim.return_value = None
        self.assertFalse(await worker.run_once())
        self.classify.assert_not_awaited()
        self.repo.finish.assert_not_awaited()

    async def test_the_model_runs_without_holding_a_connection(self):
        async def classify(*args, **kwargs):
            self.assertEqual(0, self.connections)
            return TOPICS

        self.classify.side_effect = classify
        self.assertTrue(await worker.run_once())
        self.assertEqual('test', self.repo.finish.await_args.kwargs['model'])
        self.assertNotIn('error', self.repo.finish.await_args.kwargs)

    async def test_the_backfill_runs_before_the_claim_and_carries_the_vocabulary_version(self):
        self.assertTrue(await worker.run_once())
        self.repo.backfill.assert_awaited_once()
        self.assertEqual(1, self.repo.backfill.await_args.kwargs['vocabulary_version'])
        self.assertIsInstance(self.repo.backfill.await_args.kwargs['since_ts'], int)
        self.assertEqual(1, self.repo.requeue_stale.await_args.kwargs['vocabulary_version'])

    async def test_a_failed_requeue_does_not_stop_normal_classification(self):
        self.repo.requeue_stale.side_effect = RuntimeError('locked')
        self.assertTrue(await worker.run_once())
        self.classify.assert_awaited_once()

    async def test_the_published_row_carries_topics_fingerprint_and_version(self):
        await worker.run_once()
        kwargs = self.repo.finish.await_args.kwargs
        self.assertEqual(TOPICS, kwargs['topics'])
        self.assertEqual(fingerprint('Title', 'Digest'), kwargs['fingerprint'])
        self.assertEqual(1, kwargs['vocabulary_version'])
        self.assertNotIn('error', kwargs)

    async def test_an_unchanged_article_is_not_sent_to_the_model_twice(self):
        # A row that already carries this exact fingerprint and some topics.
        self.claim.update(
            {'fingerprint': fingerprint('Title', 'Digest'), 'topics': TOPICS, 'state': 'processing'}
        )
        self.assertTrue(await worker.run_once())
        self.classify.assert_not_awaited()
        self.assertEqual(TOPICS, self.repo.finish.await_args.kwargs['topics'])

    async def test_an_empty_topic_list_is_still_published_not_retried(self):
        self.classify.return_value = []
        await worker.run_once()
        self.assertEqual([], self.repo.finish.await_args.kwargs['topics'])
        self.assertNotIn('error', self.repo.finish.await_args.kwargs)

    async def test_a_model_failure_never_publishes_partial_topics(self):
        self.classify.side_effect = ApiError('Invalid JSON', code='topic.invalid_result')
        await worker.run_once()
        kwargs = self.repo.finish.await_args.kwargs
        self.assertEqual('topic.invalid_result', kwargs['error'])
        self.assertNotIn('topics', kwargs)

    async def test_unknown_errors_do_not_leak_exception_text(self):
        self.classify.side_effect = RuntimeError('secret from upstream')
        await worker.run_once()
        self.assertEqual('topic.generation_failed', self.repo.finish.await_args.kwargs['error'])

    async def test_an_article_that_changed_during_generation_is_not_published(self):
        for rows in ([None], [self.source, {'title': 'Other', 'digest': 'Digest'}]):
            self.repo.source.side_effect = list(rows)
            self.repo.finish.reset_mock()
            await worker.run_once()
            self.assertEqual('topic.content_changed', self.repo.finish.await_args.kwargs['error'])
            self.assertNotIn('topics', self.repo.finish.await_args.kwargs)

    async def test_shutdown_requeues_the_owned_lease(self):
        self.classify.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await worker.run_once()
        self.repo.release.assert_awaited_once_with(self.claim)
        self.repo.finish.assert_not_awaited()

    async def test_total_timeout_is_a_safe_failure(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(10)

        self.classify.side_effect = slow
        with patch.object(worker, 'JOB_TIMEOUT', 0.01):
            await worker.run_once()
        self.assertEqual('topic.generation_failed', self.repo.finish.await_args.kwargs['error'])


if __name__ == '__main__':
    unittest.main()
