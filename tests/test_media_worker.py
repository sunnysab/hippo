"""Queue consumption.

The behaviour worth protecting is how the three failure kinds are routed: an
article failure spends an attempt, a service failure does not, and a dead service
stops the round instead of walking the whole queue into ``failed``.
"""

import unittest
from unittest.mock import AsyncMock, patch


def _patch_client():
    """A transcriber whose ``aclose()`` is awaitable, like the real one."""
    return patch('hippo.media.worker.client_for', return_value=AsyncMock())


from hippo.media.asr import AsrError
from hippo.media.audio import AudioError
from hippo.media.pipeline import MediaJobFailure, MediaJobSkipped
from hippo.media.worker import MAX_CONSECUTIVE_INFRA_FAILURES, drain_media_once
from hippo.media_settings import default_media_settings


class _FakeStorage:
    def __init__(self, jobs: list[dict]) -> None:
        self.commits = 0
        self.pending = list(jobs)
        self.media_jobs = AsyncMock()
        self.media_jobs.requeue_stale = AsyncMock(return_value=0)
        self.media_jobs.take_pending = AsyncMock(side_effect=self._take)

    async def _take(self, limit):
        return [self.pending.pop(0)] if self.pending else []

    async def commit(self) -> None:
        self.commits += 1


def _job(job_id: int) -> dict:
    return {'id': job_id, 'article_pk': job_id, 'source': 'podcast', 'media_url': 'https://host/a.mp3'}


def _patch_job(**behaviour):
    return patch('hippo.media.worker.transcribe_job', new=AsyncMock(**behaviour))


class DrainMediaOnceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = default_media_settings()
        self.storage = _FakeStorage([_job(1)])

    async def _drain(self, **kwargs):
        return await drain_media_once(
            storage=self.storage,
            settings=self.settings,
            resolver=AsyncMock(),
            **kwargs,
        )

    async def test_a_finished_job_is_marked_done(self) -> None:
        with (
            _patch_job(return_value={'article_pk': 1, 'segments': 3}),
            _patch_client(),
        ):
            result = await self._drain()

        self.assertEqual(1, result.done)
        self.storage.media_jobs.mark_done.assert_awaited_once_with(1)
        self.storage.media_jobs.mark_failed.assert_not_awaited()

    async def test_a_skip_is_not_a_failure(self) -> None:
        with (
            _patch_job(side_effect=MediaJobSkipped('没有音频')),
            _patch_client(),
        ):
            result = await self._drain()

        self.assertEqual(1, result.skipped)
        self.assertEqual(0, result.failed)
        self.storage.media_jobs.mark_skipped.assert_awaited_once()

    async def test_an_article_failure_spends_an_attempt(self) -> None:
        with (
            _patch_job(side_effect=MediaJobFailure('ffmpeg 处理失败')),
            _patch_client(),
        ):
            result = await self._drain()

        kwargs = self.storage.media_jobs.mark_failed.call_args.kwargs
        self.assertTrue(kwargs['retryable'])
        self.assertEqual(self.settings['transcribe_max_attempts'], kwargs['max_attempts'])
        self.assertEqual(1, result.failed + result.requeued)

    async def test_an_audio_error_is_an_article_failure(self) -> None:
        with (
            _patch_job(side_effect=AudioError('ffmpeg 处理失败')),
            _patch_client(),
        ):
            await self._drain()

        self.storage.media_jobs.mark_failed.assert_awaited_once()

    async def test_a_service_failure_goes_back_without_spending_an_attempt(self) -> None:
        with (
            _patch_job(side_effect=AsrError('无法连接转写服务')),
            _patch_client(),
        ):
            result = await self._drain()

        self.assertEqual(1, result.requeued)
        self.storage.media_jobs.mark_failed.assert_not_awaited()
        self.storage.media_jobs.requeue.assert_awaited_once()
        self.assertIsNone(result.unavailable)

    async def test_a_dead_service_stops_the_round(self) -> None:
        self.storage.pending = [_job(i) for i in range(1, 10)]
        with (
            _patch_job(side_effect=AsrError('转写服务超时')),
            _patch_client(),
        ):
            result = await self._drain(limit=9)

        self.assertIsNotNone(result.unavailable)
        self.assertEqual(MAX_CONSECUTIVE_INFRA_FAILURES, result.requeued)
        # 剩下的仍然在队列里，没被烧成 failed。
        self.assertEqual(9 - MAX_CONSECUTIVE_INFRA_FAILURES, len(self.storage.pending))

    async def test_an_unexpected_error_fails_one_article_and_the_loop_survives(self) -> None:
        self.storage.pending = [_job(1), _job(2)]
        with (
            _patch_job(side_effect=[RuntimeError('boom'), {'article_pk': 2, 'segments': 1}]),
            _patch_client(),
        ):
            result = await self._drain(limit=2)

        self.assertEqual(1, result.failed)
        self.assertEqual(1, result.done)

    async def test_a_disabled_queue_does_nothing(self) -> None:
        self.settings['enabled'] = False

        result = await self._drain()

        self.assertEqual(0, result.handled)
        self.storage.media_jobs.take_pending.assert_not_awaited()

    async def test_stale_jobs_are_requeued_before_taking_new_ones(self) -> None:
        self.storage.media_jobs.requeue_stale = AsyncMock(return_value=2)

        with (
            _patch_job(return_value={'article_pk': 1, 'segments': 1}),
            _patch_client(),
        ):
            await self._drain()

        self.storage.media_jobs.requeue_stale.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
