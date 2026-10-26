"""The loop backfills, claims one row, and never publishes a partial answer."""

import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo import topic_worker as worker
from hippo.exceptions import ApiError
from hippo.llm.client import DecisionUnavailableError, LlmUnavailableError
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
            candidates=AsyncMock(return_value=[]),
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

        self.classify = AsyncMock(return_value=(TOPICS, None))
        self.patches = [
            patch.object(worker, 'open_storage', storage),
            patch.object(worker, '_vocabulary', AsyncMock(return_value=VOCABULARY)),
            # No decision provider by default: these cases cover the chat path.
            patch.object(worker, 'resolve_decision_provider', AsyncMock(side_effect=DecisionUnavailableError('x'))),
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
            return TOPICS, None

        self.classify.side_effect = classify
        self.assertTrue(await worker.run_once())
        self.assertEqual('test', self.repo.finish.await_args.kwargs['model'])
        self.assertNotIn('error', self.repo.finish.await_args.kwargs)

    async def test_a_dry_queue_triggers_the_backfill_with_the_vocabulary_version(self):
        # The claim returns nothing, so this tick is the one that goes looking.
        self.repo.claim.side_effect = [None, self.claim]
        self.assertTrue(await worker.run_once())
        self.repo.backfill.assert_awaited_once()
        self.assertEqual(1, self.repo.backfill.await_args.kwargs['vocabulary_version'])
        self.assertIsInstance(self.repo.backfill.await_args.kwargs['since_ts'], int)
        self.assertEqual(1, self.repo.requeue_stale.await_args.kwargs['vocabulary_version'])
        # And the article it found is then classified in the same tick.
        self.classify.assert_awaited_once()

    async def test_a_busy_queue_never_scans_for_more_articles(self):
        # The backfill is a full scan of the articles table; running it per tick
        # while work is already queued made every article cost ~8s of PostgreSQL.
        self.assertTrue(await worker.run_once())
        self.repo.backfill.assert_not_awaited()

    async def test_an_exhausted_queue_stops_instead_of_classifying_nothing(self):
        self.repo.claim.side_effect = [None, None]
        self.assertFalse(await worker.run_once())
        self.repo.backfill.assert_awaited_once()
        self.classify.assert_not_awaited()

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
        self.classify.return_value = ([], None)
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

class WorkerEventTest(TopicWorkerTest):
    """The event decision rides along with the topics, in the same publish."""

    async def test_candidates_are_offered_to_the_model_and_the_decision_published(self):
        self.repo.candidates.return_value = [{'id': 4, 'title': '已有事件', 'sources': 3}]
        self.classify.return_value = (TOPICS, {'action': 'join', 'id': 4})

        self.assertTrue(await worker.run_once())

        self.assertEqual(
            [{'id': 4, 'title': '已有事件', 'sources': 3}], self.classify.await_args.kwargs['candidates']
        )
        self.assertEqual({'action': 'join', 'id': 4}, self.repo.finish.await_args.kwargs['event'])

    async def test_no_event_is_published_as_none_so_the_old_link_drops(self):
        self.classify.return_value = (TOPICS, None)
        await worker.run_once()
        self.assertIsNone(self.repo.finish.await_args.kwargs['event'])
        # The explicit flag is what lets finish tell "not a story" from "not asked".
        self.assertNotIn('keep_event', self.repo.finish.await_args.kwargs)

    async def test_a_skipped_model_call_keeps_the_existing_event(self):
        self.claim.update({'fingerprint': fingerprint('Title', 'Digest'), 'topics': TOPICS})
        await worker.run_once()

        self.classify.assert_not_awaited()
        self.assertTrue(self.repo.finish.await_args.kwargs['keep_event'])

    async def test_a_failed_classification_never_publishes_an_event(self):
        self.classify.side_effect = ApiError('bad', code='topic.invalid_result')
        await worker.run_once()
        kwargs = self.repo.finish.await_args.kwargs
        self.assertEqual('topic.invalid_result', kwargs['error'])
        self.assertNotIn('event', kwargs)

class PromptVersionTest(TopicWorkerTest):
    """The worker passes the current prompt version everywhere the row records it."""

    async def test_the_backfill_and_requeue_carry_the_prompt_version(self):
        self.repo.claim.side_effect = [None, self.claim]
        await worker.run_once()
        self.assertEqual(worker.PROMPT_VERSION, self.repo.backfill.await_args.kwargs['prompt_version'])
        self.assertEqual(worker.PROMPT_VERSION, self.repo.requeue_stale.await_args.kwargs['prompt_version'])

    async def test_a_published_row_records_the_prompt_version_that_produced_it(self):
        await worker.run_once()
        self.assertEqual(worker.PROMPT_VERSION, self.repo.finish.await_args.kwargs['prompt_version'])

class NoProviderTest(TopicWorkerTest):
    """A missing provider must pause the queue, not kill every article in it."""

    async def test_a_missing_provider_releases_the_row_instead_of_failing_it(self):
        from hippo.llm.client import LlmUnavailableError

        with (
            patch.object(worker, 'resolve_decision_provider', AsyncMock(side_effect=DecisionUnavailableError('x'))),
            patch.object(worker, 'resolve_provider', AsyncMock(side_effect=LlmUnavailableError('没有 provider'))),
        ):
            worked = await worker.run_once()

        # Not worked: the loop sleeps instead of spinning through the backlog.
        self.assertFalse(worked)
        # The row goes back to the queue: `failed` is never retried, so marking it
        # would strand every article once the provider is finally configured.
        self.repo.release.assert_awaited_once_with(self.claim)
        self.repo.finish.assert_not_awaited()
        self.classify.assert_not_awaited()

    async def test_the_row_is_not_consumed_so_it_is_classified_later(self):
        from hippo.llm.client import LlmUnavailableError

        with (
            patch.object(worker, 'resolve_decision_provider', AsyncMock(side_effect=DecisionUnavailableError('x'))),
            patch.object(worker, 'resolve_provider', AsyncMock(side_effect=LlmUnavailableError('没有 provider'))),
        ):
            await worker.run_once()

        # Nothing was written about the article itself.
        self.repo.finish.assert_not_awaited()


class DecisionProviderTest(TopicWorkerTest):
    """When a decision provider exists, the worker takes that path instead of chat."""

    async def test_a_decision_provider_is_preferred_over_a_chat_one(self):
        decision = {'name': 'clef', 'model': 'clef-flash', 'kind': 'decision'}
        with (
            patch.object(worker, 'resolve_decision_provider', AsyncMock(return_value=decision)),
            patch.object(worker, 'resolve_provider', AsyncMock(side_effect=AssertionError('chat path used'))),
            patch.object(worker, 'decide', AsyncMock(return_value={
                'ai': {'type': 'noul', 'noul': 0.93},
                'news': {'type': 'noul', 'noul': 0.1},
            })),
        ):
            self.assertTrue(await worker.run_once())

        self.classify.assert_not_awaited()
        kwargs = self.repo.finish.await_args.kwargs
        self.assertEqual(['ai'], [t['slug'] for t in kwargs['topics']])
        self.assertEqual('clef-flash', kwargs['model'])

    async def test_the_decision_call_sends_one_question_per_direction_and_one_event_question(self):
        decision = {'model': 'clef-flash', 'kind': 'decision'}
        self.repo.candidates.return_value = [{'id': 5, 'title': '已有事件', 'sources': 2}]
        decide = AsyncMock(return_value={'ai': {'type': 'noul', 'noul': 0.9},
                                         'event': {'type': 'choice', 'choice': '5', 'confidence': 0.95}})
        with (
            patch.object(worker, 'resolve_decision_provider', AsyncMock(return_value=decision)),
            patch.object(worker, 'decide', decide),
        ):
            await worker.run_once()

        questions = decide.await_args.kwargs['questions']
        self.assertIn('event', questions)
        self.assertIn('ai', questions)
        # Topics and the event decision cost one forward pass, not two calls.
        self.assertEqual(1, decide.await_count)
        self.assertEqual({'action': 'join', 'id': 5}, self.repo.finish.await_args.kwargs['event'])

    async def test_no_provider_of_either_kind_pauses_the_queue(self):
        with (
            patch.object(worker, 'resolve_decision_provider', AsyncMock(side_effect=DecisionUnavailableError('x'))),
            patch.object(worker, 'resolve_provider', AsyncMock(side_effect=LlmUnavailableError('y'))),
        ):
            self.assertFalse(await worker.run_once())
        self.repo.release.assert_awaited_once_with(self.claim)
        self.repo.finish.assert_not_awaited()


class DecisionEventCreationTest(DecisionProviderTest):
    """The decision path must be able to open an event, not only join one."""

    async def test_no_candidates_asks_the_story_question_instead_of_nothing(self):
        self.repo.candidates.return_value = []
        decide = AsyncMock(return_value={
            'ai': {'type': 'noul', 'noul': 0.9},
            'story': {'type': 'noul', 'noul': 0.85},
        })
        with (
            patch.object(worker, 'resolve_decision_provider', AsyncMock(return_value={'model': 'clef-flash', 'kind': 'decision'})),
            patch.object(worker, 'decide', decide),
        ):
            await worker.run_once()

        questions = decide.await_args.kwargs['questions']
        self.assertIn(worker.DECISION_STORY_QUESTION, questions)
        self.assertNotIn('event', questions)
        self.assertEqual('new', self.repo.finish.await_args.kwargs['event']['action'])

    async def test_the_story_question_rides_along_with_the_candidates(self):
        # Asking it only when no candidate exists deadlocked the feature: the first
        # event opened stayed a candidate forever, so every later article answered
        # "none" with no story score to fall back on, and no second event opened.
        self.repo.candidates.return_value = [{'id': 4, 'title': '已有事件', 'sources': 2}]
        decide = AsyncMock(return_value={
            'ai': {'type': 'noul', 'noul': 0.9},
            'event': {'type': 'choice', 'choice': '4', 'confidence': 0.9},
            'story': {'type': 'noul', 'noul': 0.6},
        })
        with (
            patch.object(worker, 'resolve_decision_provider', AsyncMock(return_value={'model': 'clef-flash', 'kind': 'decision'})),
            patch.object(worker, 'decide', decide),
        ):
            await worker.run_once()

        self.assertIn(worker.DECISION_STORY_QUESTION, decide.await_args.kwargs['questions'])
        self.assertEqual({'action': 'join', 'id': 4}, self.repo.finish.await_args.kwargs['event'])

    async def test_a_none_choice_can_still_open_a_new_event(self):
        # The exact deadlock: candidates exist, the model rejects all of them, and
        # the article is a story of its own.
        self.repo.candidates.return_value = [{'id': 4, 'title': '不相关事件', 'sources': 2}]
        decide = AsyncMock(return_value={
            'ai': {'type': 'noul', 'noul': 0.9},
            'event': {'type': 'choice', 'choice': 'none', 'confidence': 0.97},
            'story': {'type': 'noul', 'noul': 0.88},
        })
        with (
            patch.object(worker, 'resolve_decision_provider', AsyncMock(return_value={'model': 'clef-flash', 'kind': 'decision'})),
            patch.object(worker, 'decide', decide),
        ):
            await worker.run_once()

        self.assertEqual('new', self.repo.finish.await_args.kwargs['event']['action'])
