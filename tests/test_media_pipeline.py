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
from hippo.media.asr import AsrError, Transcript
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


def _fake_download(size: int = 1024) -> AsyncMock:
    """A stand-in for ``_download`` that leaves a real file where it says it did.

    The pipeline verifies that what it is about to transcode or store is a non-empty
    file, so a fake that only returns a number would skip the very check under test.
    """

    async def run(url, target, **_kwargs) -> int:
        target.write_bytes(b'x' * size)
        return size

    return AsyncMock(side_effect=run)


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
        # 默认没有存好的视频：走「从网上下来」那条路。用留档视频当源的测试自己覆盖。
        self.media_assets.get = AsyncMock(return_value=None)
        self.articles = AsyncMock()
        self.articles.save_article_content = AsyncMock(side_effect=self._save)

    async def _save(self, article, **kwargs):
        self.saved.append(kwargs)
        return 1

    async def commit(self) -> None:
        self.commits += 1


class _FakeTranscriber:
    def __init__(
        self,
        responses: list[str],
        *,
        error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self._responses = list(responses)
        self._error = error
        self._close_error = close_error
        self.calls = 0
        self.closes = 0
        self.max_new_tokens = 2048

    async def transcribe(self, path, **_kwargs) -> Transcript:
        self.calls += 1
        if self._error is not None:
            raise self._error
        text = self._responses.pop(0) if self._responses else ''
        return Transcript(text=text, segments=[], language='zh')

    async def aclose(self) -> None:
        self.closes += 1
        if self._close_error is not None:
            raise self._close_error


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


class CleanTranscriptTest(unittest.TestCase):
    def test_hesitations_are_dropped_wherever_they_sit(self) -> None:
        self.assertEqual(
            '这个市场的那其实过去这已经真的是一年时间了吧，就是已经完全放弃它了。',
            pipeline_module.clean_transcript(
                '这个市场的呃那其实过去这已经真的是一年时间了吧，就是已经完全呃放弃它了。'
            ),
        )

    def test_a_clause_final_particle_goes_with_its_clause(self) -> None:
        # 真实的播客转写：语气词夹在逗号前，去掉后句子照常读。
        self.assertEqual(
            '包括中概股里面的一些互联网巨头开始了解这个市场，大家最近抱怨都懒得抱怨了。',
            pipeline_module.clean_transcript(
                '包括中概股里面的一些互联网巨头开始呃了解这个市场啊，大家最近抱怨都懒得抱怨了。'
            ),
        )

    def test_a_particle_after_a_one_character_clause_is_kept(self) -> None:
        # 天啊 / 好呀 是词，不是一个停顿，删掉就变味了。
        self.assertEqual('天啊，这也太离谱了。', pipeline_module.clean_transcript('天啊，这也太离谱了。'))
        self.assertEqual('好呀，那就这么办。', pipeline_module.clean_transcript('好呀，那就这么办。'))

    def test_a_sentence_opening_filler_leaves_no_orphan_punctuation(self) -> None:
        self.assertEqual(
            '而且这三个季度是一直在连续的往下走。这个确实是。',
            pipeline_module.clean_transcript('而且这三个季度是一直在连续的往下走。 啊，这个确实是。'),
        )

    def test_a_chunk_that_is_only_filler_reads_as_empty(self) -> None:
        # 转写只有语气词时，段落为空，脚本落到「整段没转出内容」的分支。
        self.assertEqual('', pipeline_module.clean_transcript('嗯。'))
        self.assertEqual('', pipeline_module.clean_transcript('呃，嗯，啊。'))
        self.assertEqual([], pipeline_module.split_paragraphs('嗯。', 300.0))

    def test_ordinary_text_is_left_alone(self) -> None:
        text = '欢迎收听这一期节目。今天聊聊恒生科技，以及它的成分股。'
        self.assertEqual(text, pipeline_module.clean_transcript(text))


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
        # No test in this module may reach a real ASR endpoint: a transcriber is always
        # injected explicitly, and the factory is a tripwire rather than a real client.
        self.client_factory = patch(
            'hippo.media.pipeline.client_for',
            side_effect=AssertionError('client_for must not be called in this module'),
        )
        self.client_factory.start()
        self.addCleanup(self.client_factory.stop)

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
            patch('hippo.media.pipeline._download', new=_fake_download(1234)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('media/audio/7.mp3', 10)),
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

    async def test_show_notes_come_before_the_transcript(self) -> None:
        # The digest is the source's plain text, so the show notes are two literal
        # lines; both reach the body, ahead of the transcript.
        digest = 'Episode notes.\nGuest: someone'
        article = pipeline_module.ArticleRecord(**{**_ARTICLE, 'digest': digest})
        transcriber = _FakeTranscriber([_text(1900)])

        with (
            patch('hippo.media.pipeline._load_article', new=AsyncMock(return_value=article)),
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('k', 1)),
        ):
            await _run(self.storage, ResolvedMedia(audio_url='https://host/ep.mp3'), transcriber=transcriber)

        saved = self.storage.saved[0]
        blocks = saved['content_blocks']
        # Player → show notes (one paragraph block per line) → timestamped transcript.
        self.assertEqual('audio', blocks[0]['type'])
        self.assertEqual('Episode notes.', blocks[1]['text'])
        self.assertEqual('Guest: someone', blocks[2]['text'])
        self.assertNotIn('t', blocks[1])
        self.assertIn('t', blocks[3])
        # Markdown is what search and export read: notes first, transcript after.
        self.assertIn('Episode notes.', saved['content_markdown'])
        self.assertIn('Guest: someone', saved['content_markdown'])
        self.assertLess(
            saved['content_markdown'].index('Episode notes.'),
            saved['content_markdown'].index(_SENTENCE),
        )

    async def test_an_article_without_show_notes_is_unchanged(self) -> None:
        transcriber = _FakeTranscriber([_text(1900)])

        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('k', 1)),
        ):
            await _run(self.storage, ResolvedMedia(audio_url='https://host/ep.mp3'), transcriber=transcriber)

        self.assertEqual('audio', self.storage.saved[0]['content_blocks'][0]['type'])
        # No digest, no notes: the body starts at the transcript.
        self.assertTrue(self.storage.saved[0]['content_markdown'].startswith(_SENTENCE))

    async def test_show_notes_keep_a_tag_like_string(self) -> None:
        # The exact string the parent reproduced with: <label> is content, not markup,
        # so nothing between it and the ampersand may be dropped.
        digest = 'Use <label> literally & keep it'
        article = pipeline_module.ArticleRecord(**{**_ARTICLE, 'digest': digest})
        transcriber = _FakeTranscriber([_text(1900)])

        with (
            patch('hippo.media.pipeline._load_article', new=AsyncMock(return_value=article)),
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('k', 1)),
        ):
            await _run(self.storage, ResolvedMedia(audio_url='https://host/ep.mp3'), transcriber=transcriber)

        saved = self.storage.saved[0]
        self.assertEqual(digest, saved['content_blocks'][1]['text'])
        self.assertEqual(digest, saved['content_markdown'].split('\n\n')[0])
        self.assertIn(_SENTENCE, saved['content_markdown'])

    async def test_show_notes_keep_literal_angle_brackets_and_entities(self) -> None:
        # A literal tag-like word, a quoted ampersand and a written-out comparison:
        # all of it is text the reader should see exactly as the feed wrote it.
        digest = 'a <b> c & "quoted"\n5 < 6 and 7 > 4'
        article = pipeline_module.ArticleRecord(**{**_ARTICLE, 'digest': digest})
        transcriber = _FakeTranscriber([_text(1900)])

        with (
            patch('hippo.media.pipeline._load_article', new=AsyncMock(return_value=article)),
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('k', 1)),
        ):
            await _run(self.storage, ResolvedMedia(audio_url='https://host/ep.mp3'), transcriber=transcriber)

        saved = self.storage.saved[0]
        self.assertEqual('a <b> c & "quoted"', saved['content_blocks'][1]['text'])
        self.assertEqual('5 < 6 and 7 > 4', saved['content_blocks'][2]['text'])
        self.assertIn(digest, saved['content_markdown'])

    async def test_show_notes_keep_a_tag_like_string_with_a_subtitle(self) -> None:
        # Ready-made subtitle path: the tag-like digest line is untouched, and it
        # still comes before the subtitle text.
        digest = 'Use <label> literally & keep it'
        article = pipeline_module.ArticleRecord(**{**_ARTICLE, 'digest': digest})

        with (
            patch('hippo.media.pipeline._load_article', new=AsyncMock(return_value=article)),
            patch('hippo.media.pipeline._download', new=AsyncMock(return_value=1)),
        ):
            outcome = await _run(self.storage, ResolvedMedia(subtitle=_SUBTITLE))

        self.assertTrue(outcome['subtitle'])
        blocks = self.storage.saved[0]['content_blocks']
        self.assertEqual(digest, blocks[0]['text'])
        self.assertEqual('字幕', blocks[1]['text'])
        self.assertEqual(f'{digest}\n\n字幕', self.storage.saved[0]['content_markdown'])

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
            patch('hippo.media.pipeline._download', new=_fake_download(1234)) as download,
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('media/audio/7.mp3', 10)),
        ):
            outcome = await _run(self.storage, media, transcriber=transcriber)

        download.assert_awaited_once()
        self.assertEqual(0, transcriber.calls)
        self.assertTrue(outcome['archived'])
        # 播放器在正文之前，字幕段落照旧。
        self.assertEqual('audio', self.storage.saved[0]['content_blocks'][0]['type'])
        self.assertEqual('/api/media/7/audio', self.storage.saved[0]['content_blocks'][0]['url'])
        self.assertEqual('字幕', self.storage.saved[0]['content_blocks'][1]['text'])

    async def test_a_video_embed_is_never_handed_out_as_an_iframe(self) -> None:
        # Hippo plays its own copy: a stored video becomes a native block, and the
        # source's embed_url is dropped rather than passed on as a third-party iframe.
        transcriber = _FakeTranscriber([])
        media = ResolvedMedia(
            audio_url='https://host/ep.mp3',
            subtitle=_SUBTITLE,
            embed_block={
                'type': 'video',
                'provider': 'bilibili',
                'video_id': 'BV1x',
                'cover_url': 'https://cdn/cover.jpg',
                'embed_url': 'https://player.bilibili.com/x',
            },
        )
        stored = {'state': 'stored', 's3_key': 'media/video/7.bin', 'size_bytes': 16}
        self.storage.media_assets.get = AsyncMock(return_value=stored)

        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1234)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('media/audio/7.mp3', 10)),
        ):
            outcome = await _run(self.storage, media, transcriber=transcriber)

        blocks = self.storage.saved[0]['content_blocks']
        # 库里已有视频 + 已有字幕：只出原生 video 块和字幕，不给外站 iframe，
        # 也不再多存一份音频。
        self.assertEqual(['video', 'paragraph'], [block['type'] for block in blocks])
        self.assertEqual('/api/media/7/video', blocks[0]['url'])
        self.assertEqual('bilibili', blocks[0]['provider'])
        self.assertEqual('BV1x', blocks[0]['video_id'])
        self.assertEqual('https://cdn/cover.jpg', blocks[0]['cover_url'])
        self.assertNotIn('embed_url', blocks[0])
        self.assertEqual('字幕', blocks[1]['text'])
        self.assertTrue(outcome['video_stored'])

    async def test_a_video_without_a_stored_object_gets_no_video_block(self) -> None:
        # 没有真正存下来的视频就不给视频块：宁可没有播放器，也不给一个指不过去的 URL。
        transcriber = _FakeTranscriber([])
        media = ResolvedMedia(
            audio_url='https://host/ep.mp3',
            subtitle=_SUBTITLE,
            embed_block={'type': 'video', 'provider': 'bilibili', 'embed_url': 'https://player.bilibili.com/x'},
        )

        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1234)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('media/audio/7.mp3', 10)),
        ):
            outcome = await _run(self.storage, media, transcriber=transcriber)

        blocks = self.storage.saved[0]['content_blocks']
        self.assertEqual(['audio', 'paragraph'], [block['type'] for block in blocks])
        self.assertFalse(outcome['video_stored'])

    async def test_a_video_whose_audio_cannot_be_kept_still_keeps_its_source_audio(self) -> None:
        # 需要自定义头的源（B站 CDN）：留档失败时不要给出指不过去的 audio 块。
        transcriber = _FakeTranscriber([])
        media = ResolvedMedia(
            audio_url='https://cdn.bilibili.com/a.m4s',
            headers={'Referer': 'https://www.bilibili.com/'},
            subtitle=_SUBTITLE,
        )
        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1234)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('k', 1)),
            patch('hippo.media.pipeline._archive', new=AsyncMock(return_value=False)),
        ):
            await _run(self.storage, media, transcriber=transcriber)

        blocks = self.storage.saved[0]['content_blocks']
        self.assertEqual(['paragraph'], [block['type'] for block in blocks])

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

    async def test_an_entry_with_nothing_but_a_video_is_not_skipped(self) -> None:
        # 只有视频轨（没有 audio_url）也要能走 ASR：视频本身就是可转写的输入。
        media = ResolvedMedia(video_url='https://cdn.test/v.mp4', video_has_audio=True)
        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('k', 1)),
        ):
            outcome = await _run(self.storage, media, transcriber=_FakeTranscriber([_text(900)]))

        self.assertGreater(outcome['segments'], 0)

    async def test_a_too_long_recording_is_skipped_after_the_duration_is_known(self) -> None:
        settings = default_media_settings()
        settings['max_duration_seconds'] = 60
        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1)),
            self.assertRaises(MediaJobSkipped),
        ):
            await _run(self.storage, ResolvedMedia(audio_url='https://host/long.mp3'), settings=settings)

    async def test_a_stored_video_is_used_instead_of_downloading(self) -> None:
        # 已经躺在对象存储里的视频优先：那是别人手上就有的文件，不该再从网上下一遍。
        self.storage.media_assets.get = AsyncMock(
            return_value={'state': 'stored', 's3_key': 'media/video/7.webm', 'size_bytes': 4096}
        )
        fetched: list[tuple[str, str]] = []

        def _fetch(key, target, **_kwargs):
            fetched.append((key, str(target)))
            target.write_bytes(b'x' * 16)
            return 16

        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1)) as download,
            patch('hippo.media.pipeline.archive_module.fetch_object', new=_fetch),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('k', 1)),
        ):
            await _run(
                self.storage,
                ResolvedMedia(audio_url='https://host/ep.mp3'),
                transcriber=_FakeTranscriber([_text(1900)]),
            )

        download.assert_not_awaited()
        self.assertEqual(1, len(fetched))
        key, path = fetched[0]
        self.assertEqual('media/video/7.webm', key)
        # 任务自己的临时目录（hippo-media-<pk>-*），文件名叫 source.bin。
        self.assertTrue(path.endswith('source.bin'), path)
        self.assertIn('hippo-media-7-', path)

    async def test_a_repetition_loop_is_retried_as_two_halves(self) -> None:
        halves = [
            audio_module.Chunk(index=0, path=self.chunk, start_seconds=0.0, duration_seconds=150.0),
            audio_module.Chunk(index=1, path=self.chunk, start_seconds=150.0, duration_seconds=150.0),
        ]
        transcriber = _FakeTranscriber(['!' * 2048, _text(900), _text(900)])
        with (
            self._patch_audio(),
            patch('hippo.media.pipeline.audio_module.split', return_value=halves),
            patch('hippo.media.pipeline._download', new=_fake_download(1)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('k', 1)),
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
            patch('hippo.media.pipeline._download', new=_fake_download(1)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('k', 1)),
        ):
            outcome = await _run(self.storage, ResolvedMedia(audio_url='https://host/ep.mp3'), transcriber=transcriber)

        self.assertTrue(outcome['segments'] > 0)

    async def test_a_recording_that_produces_nothing_is_skipped(self) -> None:
        transcriber = _FakeTranscriber([''])
        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('k', 1)),
            self.assertRaises(MediaJobSkipped),
        ):
            await _run(self.storage, ResolvedMedia(audio_url='https://host/ep.mp3'), transcriber=transcriber)

    async def test_a_failed_archive_does_not_fail_the_transcription(self) -> None:
        transcriber = _FakeTranscriber([_text(1900)])

        def explode(**_kwargs):
            raise RuntimeError('no object store')

        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1)),
            patch('hippo.media.pipeline.archive_module.store_media', new=explode),
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

    async def test_an_injected_transcriber_is_never_replaced_by_the_factory(self) -> None:
        # 调用方传进来的 transcriber 必须留着：以前这里把它重置成 None 又去 client_for，
        # 单测因此真的发起了 HTTP 连接。工厂已换成抛异常，能转写就说明用的是注入对象。
        transcriber = _FakeTranscriber([_text(1900)])
        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('k', 1)),
        ):
            outcome = await _run(
                self.storage,
                ResolvedMedia(audio_url='https://host/ep.mp3'),
                transcriber=transcriber,
            )

        self.assertEqual(1, transcriber.calls)
        self.assertGreater(outcome['segments'], 0)

    async def test_a_ready_made_subtitle_leaves_the_factory_alone(self) -> None:
        # 有字幕就不需要 ASR：连 client_for 都不该被看一眼，更别说建客户端。
        with patch('hippo.media.pipeline._download', new=AsyncMock()) as download:
            outcome = await _run(self.storage, ResolvedMedia(subtitle=_SUBTITLE))

        download.assert_not_awaited()
        self.assertTrue(outcome['subtitle'])

    async def test_a_missing_transcriber_is_built_from_the_settings(self) -> None:
        # 唯一允许建客户端的地方：没有字幕、也没有调用方注入的对象。
        built = _FakeTranscriber([_text(1900)])
        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('k', 1)),
            patch('hippo.media.pipeline.client_for', return_value=built) as factory,
        ):
            outcome = await _run(self.storage, ResolvedMedia(audio_url='https://host/ep.mp3'))

        factory.assert_called_once()
        self.assertEqual(1, built.calls)
        self.assertGreater(outcome['segments'], 0)

    async def test_a_client_this_module_built_is_closed_on_success(self) -> None:
        built = _FakeTranscriber([_text(1900)])
        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('k', 1)),
            patch('hippo.media.pipeline.client_for', return_value=built),
        ):
            await _run(self.storage, ResolvedMedia(audio_url='https://host/ep.mp3'))

        self.assertEqual(1, built.closes)

    async def test_a_client_this_module_built_is_closed_after_a_service_error(self) -> None:
        # AsrError 也要关：worker 靠这条路径把它当作基础设施失败重排。
        built = _FakeTranscriber([], error=AsrError('cannot reach the transcription service'))
        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('k', 1)),
            patch('hippo.media.pipeline.client_for', return_value=built),
            self.assertRaises(AsrError),
        ):
            await _run(self.storage, ResolvedMedia(audio_url='https://host/ep.mp3'))

        self.assertEqual(1, built.closes)

    async def test_a_client_this_module_built_is_closed_after_a_save_failure(self) -> None:
        built = _FakeTranscriber([_text(1900)])
        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('k', 1)),
            patch('hippo.media.pipeline.client_for', return_value=built),
            patch.object(self.storage.articles, 'save_article_content', new=AsyncMock(side_effect=RuntimeError('db'))),
            self.assertRaises(RuntimeError),
        ):
            await _run(self.storage, ResolvedMedia(audio_url='https://host/ep.mp3'))

        self.assertEqual(1, built.closes)

    async def test_an_injected_client_is_never_closed_here(self) -> None:
        # 注入对象归调用者所有：pipeline 不能用完就替别人关掉。
        transcriber = _FakeTranscriber([_text(1900)])
        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('k', 1)),
        ):
            await _run(
                self.storage,
                ResolvedMedia(audio_url='https://host/ep.mp3'),
                transcriber=transcriber,
            )

        self.assertEqual(0, transcriber.closes)

    async def test_a_client_that_cannot_be_closed_does_not_mask_the_result(self) -> None:
        # 关闭失败是连接池的事，不能把已经成功的转写变成失败。
        built = _FakeTranscriber([_text(1900)], close_error=RuntimeError('socket already closed'))
        with (
            self._patch_audio(),
            patch('hippo.media.pipeline._download', new=_fake_download(1)),
            patch('hippo.media.pipeline.archive_module.store_media', new=lambda **k: ('k', 1)),
            patch('hippo.media.pipeline.client_for', return_value=built),
        ):
            outcome = await _run(self.storage, ResolvedMedia(audio_url='https://host/ep.mp3'))

        self.assertGreater(outcome['segments'], 0)
        self.assertEqual(1, built.closes)


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

    async def test_a_browser_user_agent_is_sent_unless_the_source_sets_one(self) -> None:
        # cdn.lizhi.fm 对 python-httpx/… 和 curl/… 都回 403，浏览器 UA 才给文件。
        seen: list[str] = []

        class _Recording(_RangeServer):
            def __call__(self, request: httpx.Request) -> httpx.Response:
                seen.append(request.headers.get('user-agent', ''))
                return super().__call__(request)

        with _patch_transport(_Recording(b'x' * 1024)):
            await self._fetch(_Recording(b'x' * 1024))
        self.assertTrue(seen)
        self.assertIn('Mozilla/5.0', seen[0])

        seen.clear()
        with tempfile.TemporaryDirectory() as temp_name, _patch_transport(_Recording(b'x' * 1024)):
            target = Path(temp_name) / 'a.bin'
            await _download('https://cdn.test/audio', target, headers={'User-Agent': 'yt-dlp/1'}, timeout=5)
        self.assertEqual(['yt-dlp/1'], seen)

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

        self.assertIn('ceiling', str(ctx.exception))

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

        self.assertIn('truncated', str(ctx.exception))

    async def test_an_error_status_is_reported(self) -> None:
        server = _RangeServer(b'x' * 10, status=404)

        with _patch_transport(server), self.assertRaises(MediaJobFailure) as ctx:
            await self._fetch(server)

        self.assertIn('HTTP 404', str(ctx.exception))

    async def test_an_oversized_content_length_is_refused_before_the_body(self) -> None:
        # 总长已经报出来了，不必先传完 64 KiB 再说超限。
        server = _RangeServer(os.urandom(64 * 1024))

        with _patch_transport(server), self.assertRaises(MediaJobFailure) as ctx:
            await self._fetch(server, max_bytes=4096)

        self.assertIn('4096', str(ctx.exception))

    async def test_a_restart_after_a_200_does_not_charge_the_same_bytes_twice(self) -> None:
        # 先回一个 206 的小片，接着忽略 Range 回 200 把文件重写一遍：预算是
        # 「已落盘 written + 本轨已用」，不能把第一次 200 覆盖掉的字节继续记账，
        # 否则一次回退就把配额吃掉双份。
        payload = os.urandom(32 * 1024)

        class _PartialThenWhole(_RangeServer):
            def __call__(self, request: httpx.Request) -> httpx.Response:
                header = request.headers.get('range') or ''
                start = int(header[len('bytes=') :].split('-')[0]) if header.startswith('bytes=') else 0
                if start == 0 and not self.ranges:
                    self.ranges.append((0, 4095))
                    return httpx.Response(
                        206,
                        content=payload[:4096],
                        headers={'content-length': '4096', 'content-range': f'bytes 0-4095/{len(payload)}'},
                    )
                self.ranges.append((start, len(payload) - 1))
                return httpx.Response(200, content=payload, headers={'content-length': str(len(payload))})

        server = _PartialThenWhole(payload)

        with _patch_transport(server):
            written, data = await self._fetch(server, max_bytes=len(payload))

        self.assertEqual(len(payload), written)
        self.assertEqual(payload, data)

    async def test_a_track_budget_that_is_already_spent_is_refused(self) -> None:
        # 前一条轨已经把 2 GiB 花完：第二条不能拿到一份新的。
        server = _RangeServer(os.urandom(1024))

        with _patch_transport(server), self.assertRaises(MediaJobFailure) as ctx:
            await self._fetch(server, max_bytes=4096, start_offset=4096)

        self.assertIn('4096', str(ctx.exception))

    async def test_a_wrong_content_range_start_is_not_spliced_in(self) -> None:
        # 服务器从别的地方开始回 206：拼接出来的文件是错的，必须失败而不是继续。
        class _Restarting(_RangeServer):
            def __call__(self, request: httpx.Request) -> httpx.Response:
                header = request.headers.get('range') or ''
                start = int(header[len('bytes=') :].split('-')[0]) if header.startswith('bytes=') else 0
                if start == 0 and not self.ranges:
                    # 第一块只给 1024 字节，但 Content-Range 明说这是全部 4096 字节。
                    self.ranges.append((0, 1023))
                    return httpx.Response(
                        206,
                        content=self.payload[:1024],
                        headers={'content-length': '1024', 'content-range': f'bytes 0-1023/{len(self.payload)}'},
                    )
                # 第二块请求的是 1024 起，服务器却从 0 开始回——这就是错位拼接。
                end = min(1024, len(self.payload) - 1)
                self.ranges.append((0, end))
                return httpx.Response(
                    206,
                    content=self.payload[: end + 1],
                    headers={
                        'content-length': str(end + 1),
                        'content-range': f'bytes 0-{end}/{len(self.payload)}',
                    },
                )

        payload = os.urandom(4096)
        server = _Restarting(payload)

        with _patch_transport(server), self.assertRaises(MediaJobFailure) as ctx:
            await self._fetch(server)

        self.assertIn('expected', str(ctx.exception))

    async def test_a_content_range_whose_end_precedes_its_start_is_refused(self) -> None:
        # end < start 不是一段内容，是一个不能拼接的声明。
        class _Backwards(_RangeServer):
            def __call__(self, request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    206,
                    content=self.payload[:64],
                    headers={'content-length': '64', 'content-range': f'bytes 64-32/{len(self.payload)}'},
                )

        server = _Backwards(os.urandom(1024))

        with _patch_transport(server), self.assertRaises(MediaJobFailure):
            await self._fetch(server)

    async def test_a_body_shorter_than_the_declared_range_ends_the_download(self) -> None:
        # 声明得比实际发得多：httpx 读流时报错，不能被当成完整的文件。
        class _Overclaiming(_RangeServer):
            def __call__(self, request: httpx.Request) -> httpx.Response:
                header = request.headers.get('range') or ''
                start = int(header[len('bytes=') :].split('-')[0]) if header.startswith('bytes=') else 0
                if start == 0:
                    return httpx.Response(
                        206,
                        content=self.payload[:64],
                        headers={'content-length': '1024', 'content-range': f'bytes 0-1023/{len(self.payload)}'},
                    )
                return httpx.Response(416, headers={'content-range': f'bytes */{len(self.payload)}'})

        server = _Overclaiming(os.urandom(4096))

        with _patch_transport(server), self.assertRaises(MediaJobFailure):
            await self._fetch(server)

    async def test_a_416_with_no_known_total_is_not_a_success(self) -> None:
        # 416 只说明「没有了」：一字节都没拿到时不能当成功收尾。
        class _ImmediatelyGone(_RangeServer):
            def __call__(self, request: httpx.Request) -> httpx.Response:
                return httpx.Response(416)

        server = _ImmediatelyGone(b'')

        with _patch_transport(server), self.assertRaises(MediaJobFailure) as ctx:
            await self._fetch(server)

        self.assertIn('empty file', str(ctx.exception))

    async def test_a_normal_ranged_download_is_still_accepted(self) -> None:
        # 回归保护：以上拒绝都不能把正常的 206 也一起拒掉。
        payload = os.urandom(3 * 1024)
        server = _RangeServer(payload)

        with _patch_transport(server):
            written, data = await self._fetch(server)

        self.assertEqual(len(payload), written)
        self.assertEqual(payload, data)


if __name__ == '__main__':
    unittest.main()
