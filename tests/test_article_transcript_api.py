"""Transcript export and archived-audio playback.

The export is what a script takes away, and the audio endpoint is what the reader's
player talks to — which is why the range handling gets its own tests: without it the
progress bar cannot be dragged.
"""

import unittest
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, Mock, patch

from hippo.api.routers import article as article_api
from hippo.exceptions import ApiError
from hippo.media.archive import AudioStream

BLOCKS = [
    {'type': 'video', 'video_id': 'BV1x', 'embed_url': 'https://player/bv1x'},
    {'type': 'paragraph', 'text': '第一句', 't': 0.0},
    {'type': 'paragraph', 'text': '第二句', 't': 62.5},
]


class _NullTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=1, username='u')


def _storage(*, blocks=BLOCKS, asset=None, job=None) -> SimpleNamespace:
    return SimpleNamespace(
        transaction=_NullTransaction,
        media_assets=SimpleNamespace(get=AsyncMock(return_value=asset)),
        media_jobs=AsyncMock(get=AsyncMock(return_value=job)),
        conn=None,
    )


def _row(blocks):
    return {'content_json': blocks}


class TranscriptExportTest(unittest.IsolatedAsyncioTestCase):
    async def _export(self, *, blocks=BLOCKS, fmt='md'):
        storage = _storage(blocks=blocks)
        with (
            patch('hippo.api.routers.article._assert_article_visible', new=AsyncMock()),
            patch('hippo.api.routers.article.fetchone_row', new=AsyncMock(return_value=_row(blocks))),
        ):
            return await article_api.export_transcript(
                article_id=7, format=fmt, storage=storage, user=_user()
            )

    async def test_markdown_carries_the_anchors(self) -> None:
        response = await self._export(fmt='md')

        self.assertIn('text/markdown', response.headers['content-type'])
        self.assertEqual('**[00:00]** 第一句\n\n**[01:02]** 第二句', response.body.decode())

    async def test_plain_text_drops_the_anchors(self) -> None:
        response = await self._export(fmt='txt')

        self.assertEqual('第一句\n\n第二句', response.body.decode())

    async def test_subtitles_are_numbered_cues(self) -> None:
        response = await self._export(fmt='srt')

        body = response.body.decode()
        self.assertIn('application/x-subrip', response.headers['content-type'])
        self.assertTrue(body.startswith('1\n00:00:00,000 --> 00:01:02,000\n第一句'))
        self.assertIn('2\n00:01:02,000 --> 00:01:08,000\n第二句', body)

    async def test_a_json_string_body_is_parsed(self) -> None:
        """JSONB comes back as a list, but a column read as text would not."""
        response = await self._export(blocks='[{"type":"paragraph","text":"一句","t":3}]', fmt='txt')

        self.assertEqual('一句', response.body.decode())

    async def test_an_article_without_content_is_a_404(self) -> None:
        storage = _storage()
        with (
            patch('hippo.api.routers.article._assert_article_visible', new=AsyncMock()),
            patch('hippo.api.routers.article.fetchone_row', new=AsyncMock(return_value=None)),
            self.assertRaises(ApiError) as ctx,
        ):
            await article_api.export_transcript(article_id=7, format='md', storage=storage, user=_user())

        self.assertEqual(404, ctx.exception.status)

    async def test_an_article_without_paragraphs_is_a_404(self) -> None:
        storage = _storage()
        with (
            patch('hippo.api.routers.article._assert_article_visible', new=AsyncMock()),
            patch('hippo.api.routers.article.fetchone_row', new=AsyncMock(return_value=_row('[6,7]'))),
            self.assertRaises(ApiError),
        ):
            await article_api.export_transcript(article_id=7, format='md', storage=storage, user=_user())


def _request(range_header: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(headers={'range': range_header} if range_header else {})


class OnDemandTranscribeTest(unittest.IsolatedAsyncioTestCase):
    _JOB: ClassVar[dict] = {
        'id': 1,
        'article_pk': 7,
        'source': 'bilibili',
        'media_url': 'https://www.bilibili.com/video/BV1gHps6yEHL',
        'payload': {'transcript_url': 'https://cdn/sub.vtt'},
        'state': 'done',
    }

    async def _transcribe(self, *, job=None, force_asr=False):
        storage = _storage(job=job)
        with patch('hippo.api.routers.article._assert_article_visible', new=AsyncMock()):
            result = await article_api.transcribe_article(
                article_id=7, force_asr=force_asr, storage=storage, user=_user()
            )
        return result, storage

    async def test_a_done_job_goes_back_to_pending(self) -> None:
        result, storage = await self._transcribe(job=self._JOB)

        self.assertEqual('pending', result['state'])
        queued = storage.media_jobs.enqueue_many.call_args.args[0][0]
        self.assertEqual(7, queued['article_pk'])
        self.assertEqual('bilibili', queued['source'])
        self.assertEqual(self._JOB['media_url'], queued['media_url'])
        self.assertTrue(storage.media_jobs.enqueue_many.call_args.kwargs['force'])

    async def test_force_asr_asks_the_resolver_to_skip_the_subtitle(self) -> None:
        _, storage = await self._transcribe(job=self._JOB, force_asr=True)

        queued = storage.media_jobs.enqueue_many.call_args.args[0][0]
        self.assertTrue(queued['payload']['skip_subtitle'])
        # 原来的转录稿地址留着：只是这次不用
        self.assertEqual('https://cdn/sub.vtt', queued['payload']['transcript_url'])

    async def test_an_article_without_a_job_is_a_404(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            await self._transcribe(job=None)

        self.assertEqual(404, ctx.exception.status)


class AudioStreamingTest(unittest.IsolatedAsyncioTestCase):
    async def _stream(self, *, asset, range_header=None, stream=None):
        storage = _storage(asset=asset)
        with (
            patch('hippo.api.routers.article._assert_article_visible', new=AsyncMock()),
            patch(
                'hippo.api.routers.article.archive_module.open_audio',
                new=Mock(
                    return_value=stream
                    or AudioStream(body=iter([b'x']), content_type='audio/mpeg', size=10, total=100)
                ),
            ),
        ):
            return await article_api.stream_archived_audio(
                article_pk=7, request=_request(range_header), storage=storage, user=_user()
            )

    async def test_a_stored_asset_streams_with_range_support(self) -> None:
        response = await self._stream(asset={'state': 'stored', 's3_key': 'media/audio/7.mp3'})

        self.assertEqual(200, response.status_code)
        self.assertEqual('bytes', response.headers['accept-ranges'])
        self.assertEqual('audio/mpeg', response.headers['content-type'])

    async def test_a_range_request_answers_206_with_the_bounds(self) -> None:
        response = await self._stream(
            asset={'state': 'stored', 's3_key': 'k', 'size_bytes': 100},
            range_header='bytes=10-19',
        )

        self.assertEqual(206, response.status_code)
        self.assertEqual('bytes 10-19/100', response.headers['content-range'])
        self.assertEqual('10', response.headers['content-length'])

    async def test_an_asset_without_an_archive_redirects_to_the_original(self) -> None:
        response = await self._stream(asset={'state': 'failed', 's3_key': None, 'orig_url': 'https://cdn/ep.mp3'})

        self.assertEqual(302, response.status_code)
        self.assertEqual('https://cdn/ep.mp3', response.headers['location'])

    async def test_no_asset_at_all_is_a_404(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            await self._stream(asset=None)

        self.assertEqual(404, ctx.exception.status)

    async def test_a_range_past_the_end_is_a_416(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            await self._stream(
                asset={'state': 'stored', 's3_key': 'k', 'size_bytes': 100},
                range_header='bytes=500-600',
            )

        self.assertEqual(416, ctx.exception.status)


if __name__ == '__main__':
    unittest.main()
