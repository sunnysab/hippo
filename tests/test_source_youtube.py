"""YouTube source: URL parsing, yt-dlp output mapping, and the resolver.

``run_ytdlp`` is patched everywhere rather than executed: these tests pin how the
JSON is interpreted, which is the part that breaks when yt-dlp changes.
"""

import json
import unittest
from typing import ClassVar
from unittest.mock import AsyncMock, patch

from hippo.media.pipeline import MediaJobSkipped
from hippo.sources.youtube import (
    YoutubeError,
    YoutubeResolver,
    YoutubeSource,
    best_audio_format,
    channel_id_from_url,
    parse_json_lines,
    video_block,
    video_id_from_url,
)

CHANNEL = 'UCsBjURrPoezykLs9EqgamOA'


class VideoIdTest(unittest.TestCase):
    def test_a_watch_url(self) -> None:
        self.assertEqual('WrCjAAl9okA', video_id_from_url('https://www.youtube.com/watch?v=WrCjAAl9okA'))

    def test_a_short_url(self) -> None:
        self.assertEqual('WrCjAAl9okA', video_id_from_url('https://youtu.be/WrCjAAl9okA'))

    def test_a_shorts_url(self) -> None:
        self.assertEqual('WrCjAAl9okA', video_id_from_url('https://www.youtube.com/shorts/WrCjAAl9okA'))

    def test_a_channel_url_has_no_video_id(self) -> None:
        self.assertIsNone(video_id_from_url(f'https://www.youtube.com/channel/{CHANNEL}'))

    def test_a_foreign_host_is_refused(self) -> None:
        self.assertIsNone(video_id_from_url('https://evil.example/watch?v=WrCjAAl9okA'))


class ChannelIdTest(unittest.TestCase):
    def test_a_channel_url_gives_the_id(self) -> None:
        self.assertEqual(CHANNEL, channel_id_from_url(f'https://www.youtube.com/channel/{CHANNEL}/videos'))

    def test_a_handle_url_has_no_id(self) -> None:
        self.assertIsNone(channel_id_from_url('https://www.youtube.com/@Fireship'))


class BestAudioTest(unittest.TestCase):
    def test_the_highest_bitrate_audio_only_format_wins(self) -> None:
        formats = [
            {'acodec': 'mp4a.40.2', 'vcodec': 'none', 'abr': 129.5, 'url': 'a'},
            {'acodec': 'opus', 'vcodec': 'none', 'abr': 130.0, 'url': 'b'},
            {'acodec': 'avc1', 'vcodec': 'avc1', 'abr': 0, 'url': 'muxed'},
        ]

        self.assertEqual('b', best_audio_format(formats)['url'])

    def test_a_muxed_only_video_has_no_audio_track(self) -> None:
        self.assertIsNone(best_audio_format([{'acodec': 'avc1', 'vcodec': 'avc1', 'url': 'x'}]))


class ParseJsonLinesTest(unittest.TestCase):
    def test_progress_noise_is_ignored(self) -> None:
        output = '[download] 12%\n{"id": "a"}\nnot json\n{"id": "b"}\n'

        self.assertEqual(['a', 'b'], [entry['id'] for entry in parse_json_lines(output)])


def _flat_entry(**overrides) -> dict:
    entry = {
        'id': 'WrCjAAl9okA',
        'title': '一期视频',
        'duration': 370,
        'timestamp': 1791417600,
        'thumbnails': [{'url': 'https://i.ytimg.com/vi/a/hq.jpg'}],
        'playlist_channel_id': CHANNEL,
        'playlist_channel': 'Fireship',
    }
    entry.update(overrides)
    return entry


def _ytdlp(*entries) -> AsyncMock:
    return AsyncMock(return_value='\n'.join(json.dumps(entry) for entry in entries))


class PreviewTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_channel_url_needs_no_subprocess(self) -> None:
        with patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp()) as runner:
            info = await YoutubeSource().preview(f'https://www.youtube.com/channel/{CHANNEL}')

        runner.assert_not_awaited()
        self.assertEqual(f'youtube:{CHANNEL}', info.biz)

    async def test_a_handle_is_resolved_through_yt_dlp(self) -> None:
        with patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(_flat_entry())):
            info = await YoutubeSource().preview('https://www.youtube.com/@Fireship')

        self.assertEqual(CHANNEL, info.source_key)
        self.assertEqual('Fireship', info.nickname)

    async def test_a_video_url_is_not_a_channel(self) -> None:
        with (
            patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(_flat_entry(playlist_channel_id=None))),
            self.assertRaises(YoutubeError),
        ):
            await YoutubeSource().preview('https://www.youtube.com/watch?v=WrCjAAl9okA')


class ListingTest(unittest.IsolatedAsyncioTestCase):
    async def test_entries_become_items(self) -> None:
        with patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(_flat_entry())) as runner:
            listing = await YoutubeSource().list_items(source_key=CHANNEL, limit=5)

        item = listing.items[0]
        self.assertEqual('WrCjAAl9okA', item.article_id)
        self.assertEqual('https://www.youtube.com/watch?v=WrCjAAl9okA', item.link)
        self.assertEqual(370, item.duration_seconds)
        self.assertEqual(1791417600, item.publish_at)
        self.assertEqual('https://i.ytimg.com/vi/a/hq.jpg', item.cover)
        # 列表必须显式要近似日期，否则视频会以「没有发布时间」入库
        self.assertIn('youtubetab:approximate_date', ' '.join(runner.call_args.args[0]))

    async def test_a_short_page_ends_the_listing(self) -> None:
        with patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(_flat_entry())):
            listing = await YoutubeSource().list_items(source_key=CHANNEL, limit=5)

        self.assertTrue(listing.is_end)
        self.assertIsNone(listing.next_cursor)

    async def test_a_full_page_points_at_the_next_window(self) -> None:
        with patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(_flat_entry(id='a' * 11), _flat_entry(id='b' * 11))):
            listing = await YoutubeSource().list_items(source_key=CHANNEL, limit=2)

        self.assertFalse(listing.is_end)
        self.assertEqual('3', listing.next_cursor)

    async def test_a_non_channel_key_is_refused(self) -> None:
        with self.assertRaises(YoutubeError):
            await YoutubeSource().list_items(source_key='@Fireship', limit=5)


class ResolverTest(unittest.IsolatedAsyncioTestCase):
    _JOB: ClassVar[dict] = {'media_url': 'https://www.youtube.com/watch?v=WrCjAAl9okA', 'source': 'youtube'}

    async def test_the_audio_url_travels_with_the_proxy(self) -> None:
        entry = _flat_entry(
            formats=[
                {'acodec': 'mp4a.40.2', 'vcodec': 'none', 'abr': 129.5, 'url': 'https://googlevideo/a'},
                {'acodec': 'opus', 'vcodec': 'none', 'abr': 130.0, 'url': 'https://googlevideo/b'},
            ]
        )
        with patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(entry)):
            media = await YoutubeResolver(proxy='http://proxy:8082').resolve(self._JOB)

        self.assertEqual('https://googlevideo/b', media.audio_url)
        # CDN 与站点一样不可达，代理必须跟着媒体一起走
        self.assertEqual('http://proxy:8082', media.proxy)
        self.assertEqual(370, media.duration_seconds)
        self.assertIn('/embed/WrCjAAl9okA', media.embed_block['embed_url'])

    async def test_a_muxed_only_video_is_skipped(self) -> None:
        entry = _flat_entry(formats=[{'acodec': 'avc1', 'vcodec': 'avc1', 'url': 'x'}])
        with (
            patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(entry)),
            self.assertRaises(MediaJobSkipped),
        ):
            await YoutubeResolver().resolve(self._JOB)

    async def test_an_unresolvable_url_is_skipped(self) -> None:
        with self.assertRaises(MediaJobSkipped):
            await YoutubeResolver().resolve({'media_url': 'https://www.youtube.com/@Fireship'})

    async def test_the_proxy_reaches_the_extractor(self) -> None:
        entry = _flat_entry(formats=[{'acodec': 'opus', 'vcodec': 'none', 'abr': 1.0, 'url': 'u'}])
        with patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(entry)) as runner:
            await YoutubeResolver(proxy='http://proxy:8082').resolve(self._JOB)

        self.assertEqual('http://proxy:8082', runner.call_args.kwargs['proxy'])


class VideoBlockTest(unittest.TestCase):
    def test_the_embed_url_is_built_from_the_id(self) -> None:
        block = video_block('WrCjAAl9okA', cover_url='https://i.ytimg.com/x.jpg')

        self.assertEqual('youtube', block['provider'])
        self.assertEqual('https://www.youtube.com/embed/WrCjAAl9okA', block['embed_url'])
        self.assertEqual('https://i.ytimg.com/x.jpg', block['cover_url'])


class RunYtdlpErrorTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_missing_binary_says_so(self) -> None:
        from hippo.sources.youtube import run_ytdlp

        with (
            patch('hippo.sources.youtube.asyncio.create_subprocess_exec', side_effect=FileNotFoundError),
            self.assertRaises(YoutubeError) as ctx,
        ):
            await run_ytdlp(['--version'])

        self.assertIn('yt-dlp', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
