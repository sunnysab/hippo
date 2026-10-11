"""The transcription pipeline, with the network, ffmpeg, S3 and the model faked out.

What is worth pinning down: the two paths (ready-made subtitle vs ASR), the
skips that must not become failures, and that a bad transcript is retried on
*different* input rather than the same audio again.
"""

import os
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import httpx

from hippo.media import audio as audio_module
from hippo.media import pipeline as pipeline_module
from hippo.media.asr import Transcript
from hippo.media.pipeline import (
    DOWNLOAD_BLOCK_BYTES,
    MediaJobFailure,
    MediaJobSkipped,
    ResolvedMedia,
    Subtitle,
    _download,
    transcribe_job,
)
from hippo.media_settings import default_media_settings

_ARTICLE = {
    'biz': 'podcast:abc',
    'article_id': 'ep1',
    'title': '一期播客',
    'item_show_type': None,
    'author': '某人',
    'digest': None,
    'cover': None,
    'link': 'https://example.test/ep1',
    'source_url': None,
    'publish_at': 1,
    'raw': {},
}

_SENTENCE = '这是一段用于校验切分的中文句子，长度适中。'


def _text(chars: int) -> str:
    return (_SENTENCE * (chars // len(_SENTENCE) + 1))[:chars]


class _FakeStorage:
    def __init__(self) -> None:
        self.progress: list[dict] = []
        self.saved: list[dict] = []
        self.commits = 0
        self.media_jobs = AsyncMock()
        self.media_jobs.update_progress = AsyncMock(side_effect=lambda *a, **k: self.progress.append(k))
        self.media_assets = AsyncMock()
        self.articles = AsyncMock()
        self.articles.save_article_content = AsyncMock(side_effect=self._save)

    async def _save(self, article, **kwargs):
        self.saved.append(kwargs)
        return 1

    async def commit(self) -> None:
        self.commits += 1


class _FakeTranscriber:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.max_new_tokens = 2048

    async def transcribe(self, path, **_kwargs) -> Transcript:
        self.calls += 1
        text = self._responses.pop(0) if self._responses else ''
        return Transcript(text=text, segments=[], language='zh')

    async def aclose(self) -> None:
        return None


class _Resolver:
    def __init__(self, media: ResolvedMedia) -> None:
        self._media = media

    async def resolve(self, job) -> ResolvedMedia:
        return self._media


def _run(storage, media, *, transcriber=None, settings=None, chunk_seconds=300.0):
    settings = settings or default_media_settings()
    return transcribe_job(
        storage=storage,
        job={'id': 1, 'article_pk': 7, 'source': 'podcast', 'media_url': 'https://host/ep.mp3'},
        settings=settings,
        resolver=_Resolver(media),
        transcriber=transcriber,
    )


class SplitParagraphsTest(unittest.TestCase):
    def test_sentences_are_grouped_and_timed_proportionally(self) -> None:
        paragraphs = pipeline_module.split_paragraphs(_text(600), 300.0)

        self.assertGreater(len(paragraphs), 1)
        self.assertEqual(0.0, paragraphs[0][0])
        # Timestamps increase and stay inside the chunk.
        starts = [start for start, _ in paragraphs]
        self.assertEqual(sorted(starts), starts)
        self.assertLess(starts[-1], 300.0)
        # 分组只会在「加上下一句会超长」时断开，所以最多超出一句。
        self.assertLessEqual(
            max(len(text) for _, text in paragraphs),
            pipeline_module.TARGET_PARAGRAPH_CHARS + len(_SENTENCE),
        )

    def test_empty_text_has_no_paragraphs(self) -> None:
        self.assertEqual([], pipeline_module.split_paragraphs('   ', 300.0))


_SUBTITLE = Subtitle(blocks=[{'type': 'paragraph', 'text': '字幕', 't': 0}], markdown='字幕')


class TranscribeJobTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.workdir = Path(self._dir.name)
        self.chunk = self.workdir / 'part-000.mp3'
        self.chunk.write_bytes(b'audio')
        self.storage = _FakeStorage()
        self.article = patch(
            'hippo.media.pipeline._load_article',
            new=AsyncMock(return_value=pipeline_module.ArticleRecord(**_ARTICLE)),
        )
        self.load = self.article.start()
        self.addCleanup(self.article.stop)

    def _patch_audio(self, chunks=None):
        return patch.multiple(
            pipeline_module.audio_module,
            prepare=lambda *a, **k: (
                chunks
                if chunks is not None
                else [audio_module.Chunk(index=0, path=self.chunk, start_seconds=0.0, duration_seconds=300.0)]
            ),
            probe_duration=lambda path: 300.0,
            # 下载被 patch 掉了，source 文件不存在，归档用的 ffmpeg 也得跟着假掉。
            archive=lambda *a, **k: None,
        )

    async def test_a_chunk_becomes_timestamped_paragraphs(self) -> None:
        transcriber = _FakeTranscriber([_text(1900)])
        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=AsyncMock(return_value=1234)),
            patch('hippo.media.pipeline.archive_module.store_audio', new=lambda **k: ('media/audio/7.mp3', 10)),
        ):
            outcome = await _run(self.storage, ResolvedMedia(audio_url='https://host/ep.mp3'), transcriber=transcriber)

        self.assertEqual(7, outcome['article_pk'])
        self.assertTrue(outcome['archived'])
        blocks = self.storage.saved[0]['content_blocks']
        self.assertEqual('audio', blocks[0]['type'])
        self.assertEqual('/api/media/7/audio', blocks[0]['url'])
        self.assertEqual('paragraph', blocks[1]['type'])
        self.assertIn('t', blocks[1])
        # Markdown is what search sees, so it stays free of timestamps.
        self.assertNotIn('**[', self.storage.saved[0]['content_markdown'])
        self.assertFalse(self.storage.saved[0]['replace_images'])

    async def test_a_ready_made_subtitle_skips_the_asr(self) -> None:
        transcriber = _FakeTranscriber([])
        media = ResolvedMedia(subtitle=_SUBTITLE)

        with patch('hippo.media.pipeline._download', new=AsyncMock()) as download:
            outcome = await _run(self.storage, media, transcriber=transcriber)

        download.assert_not_awaited()
        self.assertEqual(0, transcriber.calls)
        self.assertEqual([{'type': 'paragraph', 'text': '字幕', 't': 0}], self.storage.saved[0]['content_blocks'])
        self.assertFalse(outcome['archived'])

    async def test_a_ready_made_subtitle_still_archives_the_audio(self) -> None:
        # 字幕是平台给的，音频是我们唯一能留下来的东西：有字幕也要下载、转码、留档。
        transcriber = _FakeTranscriber([])
        media = ResolvedMedia(audio_url='https://host/ep.mp3', subtitle=_SUBTITLE)

        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=AsyncMock(return_value=1234)) as download,
            patch('hippo.media.pipeline.archive_module.store_audio', new=lambda **k: ('media/audio/7.mp3', 10)),
        ):
            outcome = await _run(self.storage, media, transcriber=transcriber)

        download.assert_awaited_once()
        self.assertEqual(0, transcriber.calls)
        self.assertTrue(outcome['archived'])
        # 播放器在正文之前，字幕段落照旧。
        self.assertEqual('audio', self.storage.saved[0]['content_blocks'][0]['type'])
        self.assertEqual('/api/media/7/audio', self.storage.saved[0]['content_blocks'][0]['url'])
        self.assertEqual('字幕', self.storage.saved[0]['content_blocks'][1]['text'])

    async def test_a_video_with_a_kept_audio_offers_both_players(self) -> None:
        # 「放视频、听音频、看文本」：视频条目同时给视频块与留档音频块，
        # 于是字幕锚点也就能跳（前端的 canSeek 只认 audio 块）。
        transcriber = _FakeTranscriber([])
        media = ResolvedMedia(
            audio_url='https://host/ep.mp3',
            subtitle=_SUBTITLE,
            embed_block={'type': 'video', 'provider': 'bilibili', 'embed_url': 'https://player.bilibili.com/x'},
        )
        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=AsyncMock(return_value=1234)),
            patch('hippo.media.pipeline.archive_module.store_audio', new=lambda **k: ('media/audio/7.mp3', 10)),
        ):
            outcome = await _run(self.storage, media, transcriber=transcriber)

        blocks = self.storage.saved[0]['content_blocks']
        self.assertEqual(['video', 'audio', 'paragraph'], [block['type'] for block in blocks])
        self.assertEqual('/api/media/7/audio', blocks[1]['url'])
        self.assertTrue(outcome['archived'])

    async def test_a_video_that_cannot_be_archived_gets_no_broken_player(self) -> None:
        # 需要自定义头的源（B站 CDN）：没有留档时不要给出指不过去的 audio 块。
        transcriber = _FakeTranscriber([])
        media = ResolvedMedia(
            audio_url='https://cdn.bilibili.com/a.m4s',
            headers={'Referer': 'https://www.bilibili.com/'},
            subtitle=_SUBTITLE,
            embed_block={'type': 'video', 'provider': 'bilibili', 'embed_url': 'https://player.bilibili.com/x'},
        )
        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=AsyncMock(return_value=1234)),
            patch('hippo.media.pipeline.archive_module.store_audio', new=lambda **k: ('k', 1)),
            patch('hippo.media.pipeline._archive', new=AsyncMock(return_value=False)),
        ):
            await _run(self.storage, media, transcriber=transcriber)

        blocks = self.storage.saved[0]['content_blocks']
        self.assertEqual(['video', 'paragraph'], [block['type'] for block in blocks])

    async def test_a_too_long_recording_still_keeps_its_subtitle(self) -> None:
        # 超长的音频不下载（也就不留档），但现成的字幕没理由跟着一起丢。
        settings = default_media_settings()
        settings['max_duration_seconds'] = 60
        media = ResolvedMedia(audio_url='https://host/long.mp3', duration_seconds=600, subtitle=_SUBTITLE)

        with patch('hippo.media.pipeline._download', new=AsyncMock()) as download:
            outcome = await _run(self.storage, media, settings=settings)

        download.assert_not_awaited()
        self.assertFalse(outcome['archived'])
        blocks = self.storage.saved[0]['content_blocks']
        # 超长不下载，但字幕还在；播放器指向源站（这条没有自定义头，能播）。
        self.assertEqual(['audio', 'paragraph'], [block['type'] for block in blocks])
        self.assertEqual('字幕', blocks[1]['text'])

    async def test_an_entry_without_audio_is_skipped(self) -> None:
        with self.assertRaises(MediaJobSkipped):
            await _run(self.storage, ResolvedMedia())

    async def test_a_too_long_recording_is_skipped_after_the_duration_is_known(self) -> None:
        settings = default_media_settings()
        settings['max_duration_seconds'] = 60
        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=AsyncMock(return_value=1)),
            self.assertRaises(MediaJobSkipped),
        ):
            await _run(self.storage, ResolvedMedia(audio_url='https://host/long.mp3'), settings=settings)

    async def test_a_repetition_loop_is_retried_as_two_halves(self) -> None:
        halves = [
            audio_module.Chunk(index=0, path=self.chunk, start_seconds=0.0, duration_seconds=150.0),
            audio_module.Chunk(index=1, path=self.chunk, start_seconds=150.0, duration_seconds=150.0),
        ]
        transcriber = _FakeTranscriber(['!' * 2048, _text(900), _text(900)])
        with (
            self._patch_audio(),
            patch('hippo.media.pipeline.audio_module.split', return_value=halves),
            patch('hippo.media.pipeline._download', new=AsyncMock(return_value=1)),
            patch('hippo.media.pipeline.archive_module.store_audio', new=lambda **k: ('k', 1)),
        ):
            outcome = await _run(self.storage, ResolvedMedia(audio_url='https://host/ep.mp3'), transcriber=transcriber)

        self.assertEqual(3, transcriber.calls)
        self.assertGreater(outcome['segments'], 1)

    async def test_a_silent_chunk_is_not_a_failure(self) -> None:
        transcriber = _FakeTranscriber(['', _text(900)])
        with (
            self._patch_audio(
                chunks=[
                    audio_module.Chunk(index=0, path=self.chunk, start_seconds=0.0, duration_seconds=300.0),
                    audio_module.Chunk(index=1, path=self.chunk, start_seconds=300.0, duration_seconds=300.0),
                ]
            ),
            patch('hippo.media.pipeline._download', new=AsyncMock(return_value=1)),
            patch('hippo.media.pipeline.archive_module.store_audio', new=lambda **k: ('k', 1)),
        ):
            outcome = await _run(self.storage, ResolvedMedia(audio_url='https://host/ep.mp3'), transcriber=transcriber)

        self.assertTrue(outcome['segments'] > 0)

    async def test_a_recording_that_produces_nothing_is_skipped(self) -> None:
        transcriber = _FakeTranscriber([''])
        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=AsyncMock(return_value=1)),
            patch('hippo.media.pipeline.archive_module.store_audio', new=lambda **k: ('k', 1)),
            self.assertRaises(MediaJobSkipped),
        ):
            await _run(self.storage, ResolvedMedia(audio_url='https://host/ep.mp3'), transcriber=transcriber)

    async def test_a_failed_archive_does_not_fail_the_transcription(self) -> None:
        transcriber = _FakeTranscriber([_text(1900)])

        def explode(**_kwargs):
            raise RuntimeError('no object store')

        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=AsyncMock(return_value=1)),
            patch('hippo.media.pipeline.archive_module.store_audio', new=explode),
        ):
            outcome = await _run(self.storage, ResolvedMedia(audio_url='https://host/ep.mp3'), transcriber=transcriber)

        self.assertFalse(outcome['archived'])
        # The player falls back to the original URL rather than to a missing endpoint.
        self.assertEqual('https://host/ep.mp3', self.storage.saved[0]['content_blocks'][0]['url'])
        self.storage.media_assets.mark_failed.assert_awaited()

    async def test_a_download_failure_is_an_article_failure(self) -> None:
        with (
            patch('hippo.media.pipeline._download', new=AsyncMock(side_effect=MediaJobFailure('HTTP 404'))),
            self.assertRaises(MediaJobFailure),
        ):
            await _run(self.storage, ResolvedMedia(audio_url='https://host/gone.mp3'))


if __name__ == '__main__':
    unittest.main()


class _RangeServer:
    """A stand-in for an audio host, with the two behaviours that matter."""

    def __init__(
        self,
        payload: bytes,
        *,
        supports_range: bool = True,
        fail_after: int | None = None,
        status: int = 200,
    ) -> None:
        self.payload = payload
        self.supports_range = supports_range
        self.status = status
        #: 第一次传输到这么多字节就断开，用来模拟「下到一半被掐断」。
        self.fail_after = fail_after
        self.ranges: list[tuple[int, int]] = []
        self.failures = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(self.status)
        header = request.headers.get('range') or ''
        start = 0
        if self.supports_range and header.startswith('bytes='):
            bounds = header[len('bytes=') :].split('-')
            start = int(bounds[0])
            end = min(int(bounds[1]), len(self.payload) - 1)
            self.ranges.append((start, end))
        else:
            end = len(self.payload) - 1
        if start >= len(self.payload):
            return httpx.Response(416, headers={'content-range': f'bytes */{len(self.payload)}'})

        body = self.payload[start : end + 1]
        if self.fail_after is not None and self.failures == 0 and len(body) > self.fail_after:
            # 声称的长度大于实际发出的字节：httpx 读流时报错，正是生产里
            # 「peer closed connection without sending complete message body」。
            self.failures += 1
            return httpx.Response(
                206,
                content=body[: self.fail_after],
                headers={
                    'content-length': str(len(body)),
                    'content-range': f'bytes {start}-{end}/{len(self.payload)}',
                },
            )
        headers = {'content-length': str(len(body))}
        if self.supports_range:
            headers['content-range'] = f'bytes {start}-{end}/{len(self.payload)}'
        return httpx.Response(206 if self.supports_range else 200, content=body, headers=headers)


def _patch_transport(server: _RangeServer):
    real = httpx.AsyncClient

    def factory(**kwargs):
        kwargs['transport'] = httpx.MockTransport(server)
        return real(**kwargs)

    return patch('hippo.media.pipeline.httpx.AsyncClient', side_effect=factory)


class DownloadTest(unittest.IsolatedAsyncioTestCase):
    """``_download`` walks the file in ranged blocks, and keeps what arrived."""

    async def _fetch(self, server: _RangeServer, **kwargs: object) -> tuple[int, bytes]:
        with tempfile.TemporaryDirectory() as temp_name:
            target = Path(temp_name) / 'audio.bin'
            written = await _download('https://cdn.test/audio', target, headers={}, timeout=5, **kwargs)  # type: ignore[arg-type]
            return written, target.read_bytes()

    async def test_it_fetches_a_file_larger_than_one_block(self) -> None:
        payload = os.urandom(DOWNLOAD_BLOCK_BYTES + 4096)
        server = _RangeServer(payload)

        with _patch_transport(server):
            written, data = await self._fetch(server)

        self.assertEqual(len(payload), written)
        self.assertEqual(payload, data)
        # 分块而不是一次整段：这正是绕过 YouTube 限速的原因。
        self.assertEqual(2, len(server.ranges))

    async def test_a_dropped_transfer_resumes_from_where_it_stopped(self) -> None:
        payload = os.urandom(DOWNLOAD_BLOCK_BYTES + 4096)
        server = _RangeServer(payload, fail_after=1024)

        with patch('hippo.media.pipeline.asyncio.sleep', new=AsyncMock()), _patch_transport(server):
            written, data = await self._fetch(server)

        self.assertEqual(len(payload), written)
        self.assertEqual(payload, data)
        # 第二次请求从断点开始，而不是从 0 重新下。
        self.assertEqual(1024, server.ranges[1][0])

    async def test_a_host_without_range_support_is_still_downloaded_whole(self) -> None:
        payload = os.urandom(64 * 1024)
        server = _RangeServer(payload, supports_range=False)

        with _patch_transport(server):
            written, data = await self._fetch(server)

        self.assertEqual(len(payload), written)
        self.assertEqual(payload, data)

    async def test_the_size_ceiling_still_stops_a_wrong_url(self) -> None:
        server = _RangeServer(os.urandom(64 * 1024))

        with _patch_transport(server), self.assertRaises(MediaJobFailure) as ctx:
            await self._fetch(server, max_bytes=4096)

        self.assertIn('上限', str(ctx.exception))

    async def test_a_short_answer_is_a_failure_not_a_file(self) -> None:
        # 先给出总长，随后回答说「没有了」：不能把半截当成完整文件存档。
        class _Truncating(_RangeServer):
            def __call__(self, request: httpx.Request) -> httpx.Response:
                header = request.headers.get('range') or ''
                start = int(header[len('bytes=') :].split('-')[0]) if header.startswith('bytes=') else 0
                if start == 0:
                    body = self.payload[:4096]
                    return httpx.Response(
                        206,
                        content=body,
                        headers={
                            'content-length': str(len(body)),
                            'content-range': f'bytes 0-4095/{len(self.payload)}',
                        },
                    )
                return httpx.Response(416, headers={'content-range': f'bytes */{len(self.payload)}'})

        server = _Truncating(os.urandom(64 * 1024))

        with _patch_transport(server), self.assertRaises(MediaJobFailure) as ctx:
            await self._fetch(server)

        self.assertIn('截断', str(ctx.exception))

    async def test_an_error_status_is_reported(self) -> None:
        server = _RangeServer(b'x' * 10, status=404)

        with _patch_transport(server), self.assertRaises(MediaJobFailure) as ctx:
            await self._fetch(server)

        self.assertIn('HTTP 404', str(ctx.exception))
