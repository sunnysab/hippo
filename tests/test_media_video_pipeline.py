"""The video half of the pipeline: what gets downloaded, stored and shown.

Kept apart from ``test_media_pipeline`` so the podcast/audio regressions stay
readable. These cases are the video consumption contract: a progressive file is
used as it is, split tracks are muxed, a stored video is reused, a ready-made
subtitle never initialises the ASR, and nothing ever claims a video exists that is
not in the object store.

Everything is faked at the network and object-store boundary; the files are a few
bytes each and no test here talks to a real host or a real bucket.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from hippo.media import archive as archive_module
from hippo.media import pipeline as pipeline_module
from hippo.media import video as video_module
from hippo.media.asr import Transcript
from hippo.media.pipeline import MAX_DOWNLOAD_BYTES, MediaJobFailure, MediaJobSkipped, ResolvedMedia, Subtitle
from hippo.media_settings import default_media_settings

_ARTICLE = {
    'biz': 'youtube:UC123',
    'article_id': 'vid1',
    'title': 'A video',
    'item_show_type': None,
    'author': 'someone',
    'digest': None,
    'cover': None,
    'link': 'https://example.test/vid1',
    'source_url': None,
    'publish_at': 1,
    'raw': {},
}

_SENTENCE = '这是一段用于校验切分的中文句子，长度适中。'


def _text(chars: int) -> str:
    return (_SENTENCE * (chars // len(_SENTENCE) + 1))[:chars]


_EMBED = {
    'type': 'video',
    'provider': 'youtube',
    'video_id': 'vid1',
    'cover_url': 'https://i.test/cover.jpg',
    'embed_url': 'https://www.youtube.com/embed/vid1',
}

_SUBTITLE = Subtitle(blocks=[{'type': 'paragraph', 'text': 'caption', 't': 0}], markdown='caption')


class _FakeStorage:
    def __init__(self, *, stored: dict | None = None) -> None:
        self.saved: list[dict] = []
        self.progress: list[dict] = []
        self.commits = 0
        self.media_jobs = AsyncMock()
        self.media_jobs.update_progress = AsyncMock(side_effect=lambda *a, **k: self.progress.append(k))
        self.media_assets = AsyncMock()
        self.media_assets.get = AsyncMock(return_value=stored)
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
        self.closes = 0

    async def transcribe(self, path, **_kwargs) -> Transcript:
        self.calls += 1
        text = self._responses.pop(0) if self._responses else ''
        return Transcript(text=text, segments=[], language='zh')

    async def aclose(self) -> None:
        self.closes += 1


class _Resolver:
    def __init__(self, media: ResolvedMedia) -> None:
        self._media = media

    async def resolve(self, job) -> ResolvedMedia:
        return self._media


def _fake_download(sizes: list[int] | None = None) -> AsyncMock:
    """A download stand-in that writes a real file, so the size checks stay live."""
    written: list[int] = []

    async def run(url, target, **_kwargs) -> int:
        size = sizes[len(written)] if sizes else len(url.encode())
        written.append(size)
        target.write_bytes(b'x' * size)
        return size

    return AsyncMock(side_effect=run)


def _fake_archive(source, target, **kwargs) -> None:
    """ffmpeg 抽音轨的替身：留下目标文件，让后续的上传与时长校验照常走。"""
    del kwargs
    target.write_bytes(b'a' * 256)


def _run(storage, media, *, transcriber=None, settings=None):
    return pipeline_module.transcribe_job(
        storage=storage,
        job={'id': 1, 'article_pk': 7, 'source': 'youtube', 'media_url': 'https://www.youtube.com/watch?v=x'},
        settings=settings or default_media_settings(),
        resolver=_Resolver(media),
        transcriber=transcriber,
    )


class _VideoTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.workdir = Path(self._dir.name)
        self.chunk = self.workdir / 'part-000.mp3'
        self.chunk.write_bytes(b'audio')
        loaded = patch(
            'hippo.media.pipeline._load_article',
            new=AsyncMock(return_value=pipeline_module.ArticleRecord(**_ARTICLE)),
        )
        loaded.start()
        self.addCleanup(loaded.stop)
        # No test here may build a real ASR client: the factory is a tripwire. The one
        # test that exercises construction patches it again deliberately.
        factory = patch(
            'hippo.media.pipeline.client_for',
            side_effect=AssertionError('the ASR factory must not be reached in this module'),
        )
        factory.start()
        self.addCleanup(factory.stop)
        patched_audio = patch.multiple(
            pipeline_module.audio_module,
            prepare=lambda *a, **k: [
                pipeline_module.audio_module.Chunk(index=0, path=self.chunk, start_seconds=0.0, duration_seconds=300.0)
            ],
            probe_duration=lambda path: 300.0,
            # 视频留档之后还要抽一份音频，这里不让真的 ffmpeg 跑起来。
            archive=_fake_archive,
        )
        patched_audio.start()
        self.addCleanup(patched_audio.stop)
        patched_merge = patch('hippo.media.pipeline.video_module.merge', side_effect=self._merge)
        patched_merge.start()
        self.addCleanup(patched_merge.stop)
        self.merges: list[dict] = []
        self.uploads: list[dict] = []
        patched_store = patch('hippo.media.pipeline.archive_module.store_media', side_effect=self._store_media)
        patched_store.start()
        self.addCleanup(patched_store.stop)

    def _merge(self, video, audio, target, **kwargs):
        self.merges.append({'video': video, 'audio': audio, 'target': target, **kwargs})
        target.write_bytes(b'm' * 2048)
        return 2048

    def _store_media(self, **kwargs):
        self.uploads.append(kwargs)
        return f'media/{kwargs.get("kind", "audio")}/7.bin', kwargs['path'].stat().st_size

    @staticmethod
    def _block_types(storage) -> list[str]:
        return [block['type'] for block in storage.saved[0]['content_blocks']]


class ProgressiveVideoTest(_VideoTestBase):
    async def test_a_progressive_mp4_is_stored_as_a_video_and_played_natively(self) -> None:
        # 已经带声音的渐进 MP4：不合并、不另存 MP3，直接换成原生 video 块。
        storage = _FakeStorage()
        media = ResolvedMedia(
            video_url='https://cdn.test/v.mp4',
            video_has_audio=True,
            embed_block=dict(_EMBED),
            subtitle=_SUBTITLE,
        )

        with patch('hippo.media.pipeline._download', new=_fake_download()):
            outcome = await _run(storage, media, transcriber=_FakeTranscriber([]))

        # 留档的视频之外再抽一份音频：读者多半只想听，不必为此拉整片 MP4。
        self.assertEqual(['video', 'audio', 'paragraph'], self._block_types(storage))
        block = storage.saved[0]['content_blocks'][0]
        self.assertEqual('/api/media/7/video', block['url'])
        self.assertEqual('vid1', block['video_id'])
        self.assertEqual('https://i.test/cover.jpg', block['cover_url'])
        self.assertNotIn('embed_url', block)
        self.assertEqual('/api/media/7/audio', storage.saved[0]['content_blocks'][1]['url'])
        self.assertTrue(outcome['archived'])
        self.assertTrue(outcome['video_stored'])
        # 两次落库：先视频后抽出来的音频，两者都不该被标成失败。
        saved = [call.kwargs for call in storage.media_assets.save.await_args_list]
        # 音频走仓储的默认 kind='audio'。
        self.assertEqual(
            [('video', 'video/mp4'), (None, 'audio/mpeg')], [(item.get('kind'), item['content_type']) for item in saved]
        )
        self.assertEqual([], storage.media_assets.mark_failed.await_args_list)

    async def test_a_ready_subtitle_never_starts_the_asr(self) -> None:
        # 有现成字幕就不初始化 ASR：没配 endpoint 的实例也必须能完成视频留档。
        # 基类已把 client_for 换成抛异常的哨兵，所以这条测试不需要再自己 patch。
        storage = _FakeStorage()
        media = ResolvedMedia(
            video_url='https://cdn.test/v.mp4',
            video_has_audio=True,
            embed_block=dict(_EMBED),
            subtitle=_SUBTITLE,
        )

        with patch('hippo.media.pipeline._download', new=_fake_download()):
            outcome = await _run(storage, media)

        self.assertTrue(outcome['video_stored'])
        self.assertEqual('caption', storage.saved[0]['content_blocks'][-1]['text'])


class SplitTrackVideoTest(_VideoTestBase):
    async def test_two_tracks_are_downloaded_serially_and_muxed(self) -> None:
        storage = _FakeStorage()
        media = ResolvedMedia(
            video_url='https://cdn.test/v.mp4',
            video_has_audio=False,
            audio_url='https://cdn.test/a.m4s',
            embed_block=dict(_EMBED),
            subtitle=_SUBTITLE,
        )
        download = _fake_download([16, 16])

        with patch('hippo.media.pipeline._download', new=download):
            outcome = await _run(storage, media)

        self.assertEqual(2, download.await_count)
        self.assertEqual(1, len(self.merges))
        self.assertEqual(MAX_DOWNLOAD_BYTES, self.merges[0]['max_bytes'])
        self.assertTrue(outcome['video_stored'])
        # 合并出来的 MP4 就是音频的来源，不必再多占一份下载预算。
        self.assertEqual(['video', 'audio', 'paragraph'], self._block_types(storage))

    async def test_the_second_track_carries_the_first_track_budget(self) -> None:
        # 分轨共享同一个 2 GiB 预算：第二条轨不能再拿一份新的。
        storage = _FakeStorage()
        media = ResolvedMedia(
            video_url='https://cdn.test/v.mp4',
            video_has_audio=False,
            audio_url='https://cdn.test/a.m4s',
            embed_block=dict(_EMBED),
            subtitle=_SUBTITLE,
        )
        download = _fake_download([16, 16])

        with patch('hippo.media.pipeline._download', new=download):
            await _run(storage, media)

        offsets = [call.kwargs['start_offset'] for call in download.await_args_list]
        self.assertEqual([0, 16], offsets)

    async def test_a_video_without_a_usable_audio_track_fails_instead_of_pretending(self) -> None:
        storage = _FakeStorage()
        media = ResolvedMedia(
            video_url='https://cdn.test/v.mp4',
            video_has_audio=False,
            embed_block=dict(_EMBED),
            subtitle=_SUBTITLE,
        )

        with (
            patch('hippo.media.pipeline._download', new=_fake_download()),
            self.assertRaises(MediaJobFailure),
        ):
            await _run(storage, media)

        self.assertEqual([], storage.saved)
        self.assertEqual([], storage.media_assets.save.await_args_list)


class StoredVideoReuseTest(_VideoTestBase):
    @staticmethod
    def _stored() -> dict:
        return {'state': 'stored', 's3_key': 'media/video/7.bin', 'size_bytes': 4096}

    async def test_a_stored_video_with_a_subtitle_touches_neither_network_nor_store(self) -> None:
        # 已有视频 + 已有字幕：平台和对象存储都不该被碰。
        storage = _FakeStorage(stored=self._stored())
        media = ResolvedMedia(
            video_url='https://cdn.test/v.mp4',
            video_has_audio=True,
            embed_block=dict(_EMBED),
            subtitle=_SUBTITLE,
        )

        with (
            patch('hippo.media.pipeline._download', new=_fake_download()) as download,
            # fetch_object is a synchronous function run through to_thread; an AsyncMock
            # here would hand the pipeline a coroutine and warn instead of failing.
            patch('hippo.media.pipeline.archive_module.fetch_object', new=MagicMock()) as fetch,
        ):
            outcome = await _run(storage, media)

        download.assert_not_awaited()
        fetch.assert_not_called()
        self.assertEqual([], self.uploads)
        self.assertEqual(['video', 'paragraph'], self._block_types(storage))
        self.assertEqual('/api/media/7/video', storage.saved[0]['content_blocks'][0]['url'])
        self.assertTrue(outcome['video_stored'])

    async def test_a_stored_video_can_be_the_asr_input_without_an_audio_url(self) -> None:
        # resolver 没给音频轨时，库里已有的视频自己就是转写输入，不该被 no-audio 早退拦下。
        storage = _FakeStorage(stored=self._stored())
        media = ResolvedMedia(video_url=None, audio_url=None, embed_block=dict(_EMBED))

        def _fetch(key, target, **kwargs):
            target.write_bytes(b's' * 32)
            return 32

        fetch = MagicMock(side_effect=_fetch)
        with (
            patch('hippo.media.pipeline._download', new=_fake_download()) as download,
            patch('hippo.media.pipeline.archive_module.fetch_object', new=fetch),
        ):
            outcome = await _run(storage, media, transcriber=_FakeTranscriber([_text(900)]))

        download.assert_not_awaited()
        fetch.assert_called_once()
        # 用留档视频当源也要显式带上限，不能整文件无界读。
        self.assertEqual(MAX_DOWNLOAD_BYTES, fetch.call_args.kwargs['max_bytes'])
        self.assertGreater(outcome['segments'], 0)
        self.assertTrue(outcome['video_stored'])

    async def test_an_audio_only_entry_still_uses_the_stored_video_as_its_source(self) -> None:
        storage = _FakeStorage(stored=self._stored())
        media = ResolvedMedia(audio_url='https://cdn.test/a.m4s')
        calls: list[tuple] = []

        def _fetch(key, target, **kwargs):
            calls.append((key, kwargs))
            target.write_bytes(b's' * 16)
            return 16

        with (
            patch('hippo.media.pipeline._download', new=_fake_download()) as download,
            patch('hippo.media.pipeline.archive_module.fetch_object', new=MagicMock(side_effect=_fetch)),
        ):
            outcome = await _run(storage, media, transcriber=_FakeTranscriber([_text(900)]))

        download.assert_not_awaited()
        self.assertEqual('media/video/7.bin', calls[0][0])
        self.assertTrue(outcome['video_stored'])


class VideoSkipTest(_VideoTestBase):
    async def test_keep_media_off_with_a_subtitle_downloads_nothing(self) -> None:
        # 字幕已在手上，媒体又明确不要留：不该为了「完整」白下几百兆。
        storage = _FakeStorage()
        settings = default_media_settings()
        settings['keep_media'] = False
        media = ResolvedMedia(
            video_url='https://cdn.test/v.mp4',
            video_has_audio=True,
            embed_block=dict(_EMBED),
            subtitle=_SUBTITLE,
        )

        with patch('hippo.media.pipeline._download', new=_fake_download()) as download:
            outcome = await _run(storage, media, settings=settings)

        download.assert_not_awaited()
        self.assertFalse(outcome['video_stored'])
        # 没有真正存下来的视频就不给原生块，更不给外站 iframe。
        self.assertEqual(['paragraph'], self._block_types(storage))
        self.assertEqual([], storage.media_assets.save.await_args_list)

    async def test_keep_media_off_but_an_already_stored_video_still_plays(self) -> None:
        storage = _FakeStorage(stored={'state': 'stored', 's3_key': 'k', 'size_bytes': 4096})
        settings = default_media_settings()
        settings['keep_media'] = False
        media = ResolvedMedia(
            video_url='https://cdn.test/v.mp4',
            video_has_audio=True,
            embed_block=dict(_EMBED),
            subtitle=_SUBTITLE,
        )

        with patch('hippo.media.pipeline._download', new=_fake_download()) as download:
            outcome = await _run(storage, media, settings=settings)

        download.assert_not_awaited()
        self.assertEqual(['video', 'paragraph'], self._block_types(storage))
        self.assertTrue(outcome['video_stored'])

    async def test_a_video_over_the_duration_ceiling_is_skipped(self) -> None:
        storage = _FakeStorage()
        settings = default_media_settings()
        settings['max_duration_seconds'] = 60
        media = ResolvedMedia(
            video_url='https://cdn.test/v.mp4',
            video_has_audio=True,
            duration_seconds=600,
            embed_block=dict(_EMBED),
        )

        with (
            patch('hippo.media.pipeline._download', new=_fake_download()) as download,
            self.assertRaises(MediaJobSkipped),
        ):
            await _run(storage, media, settings=settings)

        download.assert_not_awaited()

    async def test_an_over_long_stored_video_still_keeps_its_player(self) -> None:
        # 超长的条目不再下载，但库里已经有了的视频没理由跟着丢掉播放器。
        storage = _FakeStorage(stored={'state': 'stored', 's3_key': 'k', 'size_bytes': 4096})
        settings = default_media_settings()
        settings['max_duration_seconds'] = 60
        media = ResolvedMedia(
            video_url='https://cdn.test/v.mp4',
            video_has_audio=True,
            duration_seconds=600,
            embed_block=dict(_EMBED),
            subtitle=_SUBTITLE,
        )

        with patch('hippo.media.pipeline._download', new=_fake_download()) as download:
            outcome = await _run(storage, media, settings=settings)

        download.assert_not_awaited()
        self.assertEqual(['video', 'paragraph'], self._block_types(storage))
        self.assertTrue(outcome['video_stored'])


class StoredVideoAsrInputTest(_VideoTestBase):
    """The construction path, kept apart because the base class forbids it elsewhere."""

    async def test_a_missing_transcriber_is_built_once_and_used(self) -> None:
        storage = _FakeStorage(stored={'state': 'stored', 's3_key': 'media/video/7.bin', 'size_bytes': 4096})
        media = ResolvedMedia(video_url=None, audio_url=None, embed_block=dict(_EMBED))
        built = _FakeTranscriber([_text(900)])

        def _fetch(key, target, **_kwargs):
            target.write_bytes(b's' * 32)
            return 32

        with (
            patch('hippo.media.pipeline.archive_module.fetch_object', new=MagicMock(side_effect=_fetch)),
            patch('hippo.media.pipeline.client_for', return_value=built) as factory,
        ):
            outcome = await _run(storage, media)

        factory.assert_called_once()
        self.assertEqual(1, built.calls)
        # A client this module built is closed once, even on the happy path.
        self.assertEqual(1, built.closes)
        self.assertGreater(outcome['segments'], 0)

    async def test_the_client_it_builds_is_closed_when_the_job_fails(self) -> None:
        storage = _FakeStorage(stored={'state': 'stored', 's3_key': 'media/video/7.bin', 'size_bytes': 4096})
        media = ResolvedMedia(video_url=None, audio_url=None, embed_block=dict(_EMBED))
        built = _FakeTranscriber([''])

        def _fetch(key, target, **_kwargs):
            target.write_bytes(b's' * 32)
            return 32

        with (
            patch('hippo.media.pipeline.archive_module.fetch_object', new=MagicMock(side_effect=_fetch)),
            patch('hippo.media.pipeline.client_for', return_value=built),
            self.assertRaises(MediaJobSkipped),
        ):
            await _run(storage, media)

        # The empty transcript skips the job, and the connection is still released.
        self.assertEqual(1, built.closes)


class VideoErrorMappingTest(_VideoTestBase):
    """The two seams that must reach the queue's retry path, not a silent success."""

    async def test_a_merge_failure_becomes_a_retryable_article_failure(self) -> None:
        storage = _FakeStorage()
        media = ResolvedMedia(
            video_url='https://cdn.test/v.mp4',
            video_has_audio=False,
            audio_url='https://cdn.test/a.m4s',
            embed_block=dict(_EMBED),
            subtitle=_SUBTITLE,
        )

        with (
            patch('hippo.media.pipeline._download', new=_fake_download([16, 16])),
            patch(
                'hippo.media.pipeline.video_module.merge',
                new=MagicMock(side_effect=video_module.VideoError('ffmpeg failed (exit 1)')),
            ),
            self.assertRaises(MediaJobFailure) as ctx,
        ):
            await _run(storage, media)

        self.assertIn('merge failed', str(ctx.exception))
        self.assertEqual([], storage.saved)
        self.assertEqual([], self.uploads)

    async def test_an_archive_read_failure_becomes_a_retryable_article_failure(self) -> None:
        storage = _FakeStorage(stored={'state': 'stored', 's3_key': 'media/video/7.bin', 'size_bytes': 4096})
        media = ResolvedMedia(video_url=None, audio_url=None)

        with (
            patch(
                'hippo.media.pipeline.archive_module.fetch_object',
                new=MagicMock(side_effect=archive_module.ArchiveError('archived media incomplete: 0/4096 bytes')),
            ),
            self.assertRaises(MediaJobFailure) as ctx,
        ):
            await _run(storage, media, transcriber=_FakeTranscriber([_text(900)]))

        self.assertIn('archived video', str(ctx.exception))
        self.assertEqual([], storage.saved)


class VideoStorageFailureTest(_VideoTestBase):
    async def test_an_upload_failure_marks_video_failed_and_retries_the_job(self) -> None:
        storage = _FakeStorage()
        media = ResolvedMedia(
            video_url='https://cdn.test/v.mp4',
            video_has_audio=True,
            embed_block=dict(_EMBED),
            subtitle=_SUBTITLE,
        )

        def _explode(**_kwargs):
            raise RuntimeError('access denied')

        with (
            patch('hippo.media.pipeline._download', new=_fake_download()),
            patch('hippo.media.pipeline.archive_module.store_media', new=_explode),
            self.assertRaises(MediaJobFailure),
        ):
            await _run(storage, media)

        failed = storage.media_assets.mark_failed.await_args.kwargs
        self.assertEqual('video', failed['kind'])
        self.assertIn('access denied', failed['error'])
        # 失败就不许写正文：没有对象却给出 /api/media/7/video 才是真的坏。
        self.assertEqual([], storage.saved)
        self.assertEqual([], storage.media_assets.save.await_args_list)

    async def test_a_video_that_arrives_empty_is_not_stored(self) -> None:
        storage = _FakeStorage()
        media = ResolvedMedia(
            video_url='https://cdn.test/v.mp4',
            video_has_audio=True,
            embed_block=dict(_EMBED),
            subtitle=_SUBTITLE,
        )

        with patch('hippo.media.pipeline._download', new=_fake_download([0])), self.assertRaises(MediaJobFailure):
            await _run(storage, media)

        self.assertEqual([], self.uploads)

    async def test_a_merged_file_over_the_ceiling_is_not_stored(self) -> None:
        # 合并结果自己也要过非空/限额校验，不因为 merge 走了 fake 就跳过。
        storage = _FakeStorage()
        media = ResolvedMedia(
            video_url='https://cdn.test/v.mp4',
            video_has_audio=False,
            audio_url='https://cdn.test/a.m4s',
            embed_block=dict(_EMBED),
            subtitle=_SUBTITLE,
        )

        with (
            patch('hippo.media.pipeline._download', new=_fake_download([16, 16])),
            # 2 GiB 的校验不能被降级成「都过」，所以把上限缩到能触发它的大小。
            patch('hippo.media.pipeline.MAX_DOWNLOAD_BYTES', 1024),
            patch(
                'hippo.media.pipeline.video_module.merge',
                side_effect=lambda video, audio, target, **kwargs: target.write_bytes(b'm' * 4096),
            ),
            self.assertRaises(MediaJobFailure),
        ):
            await _run(storage, media)

        self.assertEqual([], self.uploads)


if __name__ == '__main__':
    unittest.main()
