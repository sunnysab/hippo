"""Transcript export and archived-audio playback.

The export is what a script takes away, and the audio endpoint is what the reader's
player talks to — which is why the range handling gets its own tests: without it the
progress bar cannot be dragged.
"""

import json
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
            return await article_api.export_transcript(article_id=7, format=fmt, storage=storage, user=_user())

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


class _FakeBody:
    """The bits of ``botocore``'s StreamingBody the media route relies on."""

    def __init__(self, chunks, *, fail_at: int | None = None) -> None:
        self._chunks = list(chunks)
        self._fail_at = fail_at
        self.closed = False

    def __iter__(self):
        for index, chunk in enumerate(self._chunks):
            if self._fail_at is not None and index >= self._fail_at:
                raise OSError('connection reset')
            yield chunk

    def close(self) -> None:
        self.closed = True


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
    _STORED_VIDEO: ClassVar[dict] = {'state': 'stored', 's3_key': 'media/video/7.mp4', 'size_bytes': 100}

    async def _stream(self, *, asset, range_header=None, stream=None):
        storage = _storage(asset=asset)
        with (
            patch('hippo.api.routers.article._assert_article_visible', new=AsyncMock()),
            patch(
                'hippo.api.routers.article.archive_module.open_audio',
                new=Mock(
                    return_value=stream or AudioStream(body=iter([b'x']), content_type='audio/mpeg', size=10, total=100)
                ),
            ),
        ):
            return await article_api.stream_archived_media(
                kind='audio',
                article_pk=7,
                request=_request(range_header),
                storage=storage,
                user=_user(),
            )

    async def _stream_video(self, *, asset, range_header=None, stream=None):
        """Stream a video and hand back the ``open_audio`` spy and the storage."""
        storage = _storage(asset=asset)
        opened = Mock(
            return_value=stream or AudioStream(body=iter([b'x']), content_type='video/mp4', size=10, total=100)
        )
        with (
            patch('hippo.api.routers.article._assert_article_visible', new=AsyncMock()),
            patch('hippo.api.routers.article.archive_module.open_audio', new=opened),
        ):
            response = await article_api.stream_archived_media(
                kind='video',
                article_pk=7,
                request=_request(range_header),
                storage=storage,
                user=_user(),
            )
        return response, opened, storage

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

    async def test_the_same_handler_serves_video(self) -> None:
        # 音频和视频只差一个 kind：都是对象存储里的一个对象 + 一个 content type。
        storage = _storage(asset={'state': 'stored', 's3_key': 'media/video/7.mp4', 'size_bytes': 2048})
        with (
            patch('hippo.api.routers.article._assert_article_visible', new=AsyncMock()),
            patch(
                'hippo.api.routers.article.archive_module.open_audio',
                new=Mock(return_value=AudioStream(body=iter([b'x']), content_type='video/mp4', size=2048, total=2048)),
            ),
        ):
            response = await article_api.stream_archived_media(
                kind='video',
                article_pk=7,
                request=_request(),
                storage=storage,
                user=_user(),
            )

        self.assertEqual('video/mp4', response.media_type)
        self.assertEqual('video', storage.media_assets.get.await_args.kwargs['kind'])

    async def test_an_unknown_kind_is_a_404(self) -> None:
        with self.assertRaises(article_api.ApiError):
            await article_api.stream_archived_media(
                kind='wallpaper',
                article_pk=7,
                request=_request(),
                storage=_storage(),
                user=_user(),
            )

    async def test_no_asset_at_all_is_a_404(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            await self._stream(asset=None)

        self.assertEqual(404, ctx.exception.status)

    async def test_a_range_past_the_end_is_a_416_with_the_real_length(self) -> None:
        response, opened, _ = await self._stream_video(
            asset={'state': 'stored', 's3_key': 'k', 'size_bytes': 100},
            range_header='bytes=500-600',
        )

        self.assertEqual(416, response.status_code)
        self.assertEqual('bytes */100', response.headers['content-range'])
        self.assertEqual({'error': '请求的范围超出文件大小', 'code': 'http_416'}, json.loads(response.body))
        opened.assert_not_called()

    async def test_a_suffix_range_asks_the_store_for_the_last_bytes(self) -> None:
        stream = AudioStream(body=iter([b'x' * 10]), content_type='video/mp4', size=10, total=100)

        response, opened, _ = await self._stream_video(
            asset={'state': 'stored', 's3_key': 'k', 'size_bytes': 100},
            range_header='bytes=-10',
            stream=stream,
        )

        self.assertEqual(206, response.status_code)
        self.assertEqual('bytes 90-99/100', response.headers['content-range'])
        self.assertEqual('10', response.headers['content-length'])
        self.assertEqual('private, max-age=3600', response.headers['cache-control'])
        opened.assert_called_once_with('k', start=90, end=99)

    async def test_a_suffix_range_without_a_known_size_serves_the_whole_file(self) -> None:
        # The length cannot be computed from an unknown total, and a made-up 206
        # would hand the player a range it never asked the store for.
        response, opened, _ = await self._stream_video(
            asset={'state': 'stored', 's3_key': 'k', 'size_bytes': None},
            range_header='bytes=-10',
        )

        self.assertEqual(200, response.status_code)
        opened.assert_called_once_with('k', start=None, end=None)

    async def test_a_video_without_stored_bytes_never_redirects_to_the_source_page(self) -> None:
        # The original URL of a video is a third-party HTML page: a 302 would hand
        # the reader markup instead of the "no player" answer.
        storage = _storage(asset={'state': 'failed', 's3_key': None, 'orig_url': 'https://www.bilibili.com/video/BV1'})
        opened = Mock()
        with (
            patch('hippo.api.routers.article._assert_article_visible', new=AsyncMock()),
            patch('hippo.api.routers.article.archive_module.open_audio', new=opened),
            self.assertRaises(ApiError) as ctx,
        ):
            await article_api.stream_archived_media(
                kind='video',
                article_pk=7,
                request=_request(),
                storage=storage,
                user=_user(),
            )

        self.assertEqual(404, ctx.exception.status)
        opened.assert_not_called()
        self.assertEqual('video', storage.media_assets.get.await_args.kwargs['kind'])

    async def test_a_foreign_article_is_refused_before_the_store_is_touched(self) -> None:
        storage = _storage(asset=self._STORED_VIDEO)
        with (
            patch(
                'hippo.api.routers.article._assert_article_visible',
                new=AsyncMock(side_effect=ApiError('Article not found', status=404)),
            ),
            self.assertRaises(ApiError) as ctx,
        ):
            await article_api.stream_archived_media(
                kind='video',
                article_pk=7,
                request=_request(),
                storage=storage,
                user=_user(),
            )

        self.assertEqual(404, ctx.exception.status)
        storage.media_assets.get.assert_not_awaited()

    async def test_a_client_that_stops_iterating_closes_the_upstream_body(self) -> None:
        body = _FakeBody([b'a', b'b', b'c'])
        response, _opened, _ = await self._stream_video(
            asset=self._STORED_VIDEO,
            stream=AudioStream(body=body, content_type='video/mp4', size=3, total=3),
        )
        iterator = response.body_iterator
        self.assertEqual(b'a', await iterator.__anext__())

        await iterator.aclose()  # what Starlette does when the client disconnects

        self.assertTrue(body.closed)

    async def test_a_broken_upstream_body_is_closed_before_the_error_surfaces(self) -> None:
        body = _FakeBody([b'a', b'b'], fail_at=1)
        response, _opened, _ = await self._stream_video(
            asset=self._STORED_VIDEO,
            stream=AudioStream(body=body, content_type='video/mp4', size=10, total=10),
        )
        iterator = response.body_iterator
        self.assertEqual(b'a', await iterator.__anext__())

        with self.assertRaises(OSError):
            await iterator.__anext__()

        self.assertTrue(body.closed)


if __name__ == '__main__':
    unittest.main()
