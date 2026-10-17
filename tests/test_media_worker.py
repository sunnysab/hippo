"""Queue consumption.

The behaviour worth protecting is how the three failure kinds are routed: an
article failure spends an attempt, a service failure does not, and a dead service
stops the round instead of walking the whole queue into ``failed``.

The ASR client belongs to the pipeline now: the worker never builds one, so a job
that needs no transcription never touches the ASR configuration at all.
"""

import unittest
from unittest.mock import AsyncMock, patch

from hippo.media import pipeline as pipeline_module
from hippo.media.asr import AsrError
from hippo.media.audio import AudioError
from hippo.media.pipeline import MediaJobFailure, MediaJobSkipped, ResolvedMedia, Subtitle
from hippo.media.worker import MAX_CONSECUTIVE_INFRA_FAILURES, drain_media_once
from hippo.media_settings import default_media_settings


def _no_asr_factory():
    """A tripwire: a worker that builds a client itself would hit it immediately."""
    return patch(
        'hippo.media.pipeline.client_for',
        side_effect=AssertionError('the pipeline must build the ASR client, not the worker'),
    )


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
            _no_asr_factory(),
        ):
            result = await self._drain()

        self.assertEqual(1, result.done)
        self.storage.media_jobs.mark_done.assert_awaited_once_with(1)
        self.storage.media_jobs.mark_failed.assert_not_awaited()

    async def test_the_worker_never_builds_or_closes_a_transcriber(self) -> None:
        # The client belongs to the pipeline: the worker neither creates one nor closes
        # one. A factory patched onto the worker module would only be reached if the
        # worker still built a client (or closed it).
        transcriber = AsyncMock()
        factory = AsyncMock(return_value=transcriber)
        with (
            patch('hippo.media.worker.transcribe_job', new=AsyncMock(return_value={'article_pk': 1})),
            patch.object(pipeline_module, 'client_for', factory),
            patch('hippo.media.worker.client_for', create=True, new=factory),
        ):
            result = await self._drain()

        self.assertEqual(1, result.done)
        factory.assert_not_awaited()
        transcriber.aclose.assert_not_awaited()

    async def test_a_skip_is_not_a_failure(self) -> None:
        with (
            _patch_job(side_effect=MediaJobSkipped('no audio')),
            _no_asr_factory(),
        ):
            result = await self._drain()

        self.assertEqual(1, result.skipped)
        self.assertEqual(0, result.failed)
        self.storage.media_jobs.mark_skipped.assert_awaited_once()

    async def test_an_article_failure_spends_an_attempt(self) -> None:
        with (
            _patch_job(side_effect=MediaJobFailure('ffmpeg failed')),
            _no_asr_factory(),
        ):
            result = await self._drain()

        kwargs = self.storage.media_jobs.mark_failed.call_args.kwargs
        self.assertTrue(kwargs['retryable'])
        self.assertEqual(self.settings['transcribe_max_attempts'], kwargs['max_attempts'])
        self.assertEqual(1, result.failed + result.requeued)

    async def test_an_audio_error_is_an_article_failure(self) -> None:
        with (
            _patch_job(side_effect=AudioError('ffmpeg failed')),
            _no_asr_factory(),
        ):
            await self._drain()

        self.storage.media_jobs.mark_failed.assert_awaited_once()

    async def test_a_service_failure_goes_back_without_spending_an_attempt(self) -> None:
        with (
            _patch_job(side_effect=AsrError('cannot reach the transcription service')),
            _no_asr_factory(),
        ):
            result = await self._drain()

        self.assertEqual(1, result.requeued)
        self.storage.media_jobs.mark_failed.assert_not_awaited()
        self.storage.media_jobs.requeue.assert_awaited_once()
        self.assertIsNone(result.unavailable)

    async def test_a_dead_service_stops_the_round(self) -> None:
        self.storage.pending = [_job(i) for i in range(1, 10)]
        with (
            _patch_job(side_effect=AsrError('transcription service timed out')),
            _no_asr_factory(),
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
            _no_asr_factory(),
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
            _no_asr_factory(),
        ):
            await self._drain()

        self.storage.media_jobs.requeue_stale.assert_awaited_once()


class _FakeResolver:
    """A resolver that hands back one fixed set of media, as a source adapter would."""

    def __init__(self, media: ResolvedMedia) -> None:
        self._media = media

    async def resolve(self, job) -> ResolvedMedia:
        return self._media


class WorkerPipelineSeamTest(unittest.IsolatedAsyncioTestCase):
    """The worker and the real pipeline together, with only the sources faked."""

    def setUp(self) -> None:
        self.settings = default_media_settings()
        self.storage = _FakeStorage([_job(1)])
        self.storage.media_assets = AsyncMock()
        self.storage.media_assets.get = AsyncMock(return_value=None)
        self.storage.articles = AsyncMock()
        self.storage.articles.save_article_content = AsyncMock(return_value=1)
        article = patch(
            'hippo.media.pipeline._load_article',
            new=AsyncMock(
                return_value=pipeline_module.ArticleRecord(
                    biz='bilibili:BV1',
                    article_id='BV1',
                    title='a video',
                    item_show_type=None,
                    author=None,
                    digest=None,
                    cover=None,
                    link='https://example.test/BV1',
                    source_url=None,
                    publish_at=1,
                    raw={},
                )
            ),
        )
        article.start()
        self.addCleanup(article.stop)

    async def _drain(self, media: ResolvedMedia):
        return await drain_media_once(
            storage=self.storage,
            settings=self.settings,
            resolver=_FakeResolver(media),
        )

    async def test_a_cc_subtitle_job_finishes_with_the_asr_factory_untouched(self) -> None:
        # 有 CC 字幕、但没有配置 ASR endpoint 的实例：归档必须照样完成，
        # 工厂一旦被调用就抛异常，所以「完成」本身就证明没有初始化 ASR。
        content = Subtitle(blocks=[{'type': 'paragraph', 'text': 'caption', 't': 0}], markdown='caption')
        media = ResolvedMedia(audio_url='https://cdn.test/a.m4s', subtitle=content)

        async def _download(url, target, **_kwargs):
            target.write_bytes(b'x' * 1024)
            return 1024

        with (
            patch('hippo.media.pipeline._download', new=AsyncMock(side_effect=_download)),
            patch.multiple(
                pipeline_module.audio_module,
                archive=lambda *a, **k: None,
                probe_duration=lambda path: 300.0,
            ),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('media/audio/1.mp3', 1024)),
            patch(
                'hippo.media.pipeline.client_for',
                side_effect=AssertionError('no ASR client may be built for a CC job'),
            ),
        ):
            result = await self._drain(media)

        self.assertEqual(1, result.done)
        self.storage.media_jobs.mark_done.assert_awaited_once_with(1)
        self.storage.media_jobs.mark_failed.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
