"""YouTube source: URL parsing, yt-dlp output mapping, and the resolver.

``run_ytdlp`` is patched everywhere rather than executed: these tests pin how the
JSON is interpreted, which is the part that breaks when yt-dlp changes.
"""

import json
import stat
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, patch

from hippo.media.pipeline import MediaJobSkipped
from hippo.sources.youtube import (
    YoutubeError,
    YoutubeResolver,
    YoutubeSource,
    best_audio_format,
    best_video_format,
    channel_id_from_url,
    parse_json_lines,
    run_ytdlp,
    video_block,
    video_id_from_url,
    write_cookie_file,
    ytdlp_bin,
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

    def test_the_codec_filter_keeps_only_aac(self) -> None:
        formats = [
            {'acodec': 'opus', 'vcodec': 'none', 'abr': 130.0, 'url': 'opus'},
            {'acodec': 'mp4a.40.2', 'vcodec': 'none', 'abr': 129.5, 'url': 'aac'},
        ]

        self.assertEqual('aac', best_audio_format(formats, codec='mp4a')['url'])
        # 不带过滤时旧行为不变：最高码率的音轨胜出。
        self.assertEqual('opus', best_audio_format(formats)['url'])

    def test_the_url_filter_skips_a_manifest_without_hiding_a_usable_track(self) -> None:
        formats = [
            {'acodec': 'mp4a.40.2', 'vcodec': 'none', 'abr': 160.0, 'protocol': 'https', 'url': 'https://cdn/x.m3u8'},
            {'acodec': 'mp4a.40.2', 'vcodec': 'none', 'abr': 64.0, 'protocol': 'https', 'url': 'https://cdn/x.m4a'},
        ]

        self.assertEqual('https://cdn/x.m4a', best_audio_format(formats, codec='mp4a', urls_only=True)['url'])
        # 不过滤 URL 时旧默认语义不变。
        self.assertEqual('https://cdn/x.m3u8', best_audio_format(formats, codec='mp4a')['url'])


def _video(**overrides) -> dict:
    """A yt-dlp video format as it really looks (fields that matter here)."""
    fmt = {
        'format_id': '137',
        'ext': 'mp4',
        'vcodec': 'avc1.640028',
        'acodec': 'none',
        'width': 1280,
        'height': 720,
        'protocol': 'https',
        'url': 'https://googlevideo/video',
    }
    fmt.update(overrides)
    return fmt


def _audio_only(**overrides) -> dict:
    fmt = {
        'format_id': '140',
        'ext': 'm4a',
        'vcodec': 'none',
        'acodec': 'mp4a.40.2',
        'abr': 129.5,
        'protocol': 'https',
        'url': 'https://googlevideo/aac',
    }
    fmt.update(overrides)
    return fmt


class BestVideoTest(unittest.TestCase):
    """Only a track the pipeline can download, mux and serve may be picked."""

    def test_a_track_above_720p_is_refused(self) -> None:
        self.assertIsNone(best_video_format([_video(height=1080)]))

    def test_the_tallest_track_within_the_ceiling_wins(self) -> None:
        formats = [
            _video(height=720, url='https://cdn.test/720.mp4'),
            _video(height=360, url='https://cdn.test/360.mp4'),
            _video(height=1080, url='https://cdn.test/1080.mp4'),
        ]

        self.assertEqual('https://cdn.test/720.mp4', best_video_format(formats)['url'])

    def test_vp9_is_refused(self) -> None:
        self.assertIsNone(best_video_format([_video(vcodec='vp09.00.51.08', ext='webm')]))

    def test_a_drm_track_is_refused(self) -> None:
        self.assertIsNone(best_video_format([_video(has_drm=True)]))

    def test_an_hls_manifest_is_refused_despite_an_https_url(self) -> None:
        # HLS 的 URL 也以 https 开头：只看协议名，不能只看链接。
        hls = _video(protocol='m3u8_native', url='https://manifest.googlevideo.com/api/index.m3u8')

        self.assertIsNone(best_video_format([hls]))

    def test_a_dash_manifest_and_a_fragment_list_are_refused(self) -> None:
        self.assertIsNone(
            best_video_format(
                [
                    _video(protocol='https', manifest_url='https://manifest.googlevideo.com/dash'),
                    _video(protocol='https', fragments=[{'url': 'https://googlevideo/frag-0'}]),
                ]
            )
        )

    def test_a_progressive_track_with_its_own_audio_is_eligible(self) -> None:
        progressive = _video(acodec='mp4a.40.2', ext='mp4', height=720)

        self.assertEqual('https://googlevideo/video', best_video_format([progressive])['url'])

    def test_an_empty_or_foreign_url_is_refused(self) -> None:
        self.assertIsNone(best_video_format([_video(url='')]))
        self.assertIsNone(best_video_format([_video(url='rtmp://googlevideo/live')]))
        self.assertIsNone(best_video_format([_video(url=None)]))

    def test_a_manifest_path_under_https_is_refused(self) -> None:
        # protocol 填 https 也不能放过路径就是清单的条目（即便带查询串）。
        self.assertIsNone(best_video_format([_video(url='https://cdn.test/index.m3u8?token=1')]))
        self.assertIsNone(best_video_format([_video(url='https://cdn.test/index.mpd')]))

    def test_a_url_without_a_host_is_refused(self) -> None:
        self.assertIsNone(best_video_format([_video(url='https:///video.mp4')]))

    def test_a_non_aac_progressive_is_first_chosen_for_a_split_mux(self) -> None:
        # 图轨包着 opus 的渐进式仍然可选：拿到视频轨后由 resolver 拿独立 AAC 合流。
        track = best_video_format([_video(acodec='opus')])

        self.assertIsNotNone(track)
        self.assertEqual('opus', track['acodec'])

    def test_at_the_same_height_aac_in_the_file_wins(self) -> None:
        formats = [
            _video(acodec='opus', url='https://cdn.test/opus.mp4'),
            _video(acodec='mp4a.40.2', url='https://cdn.test/aac.mp4'),
        ]

        self.assertEqual('https://cdn.test/aac.mp4', best_video_format(formats)['url'])

    def test_require_audio_demands_aac_in_the_same_file(self) -> None:
        self.assertIsNone(best_video_format([_video(acodec='opus')], require_audio=True))
        self.assertIsNone(best_video_format([_video()], require_audio=True))
        self.assertEqual(720, best_video_format([_video(acodec='mp4a.40.2')], require_audio=True)['height'])

    def test_the_height_field_must_be_usable(self) -> None:
        self.assertIsNone(best_video_format([_video(height=None)]))
        self.assertIsNone(best_video_format([_video(height='')]))


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
        # 渐进式以外的 muxed 条目按老行为仍然跳过（拿不到可靠形状）。
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

    async def test_the_cookie_and_the_metadata_only_command_are_unchanged(self) -> None:
        entry = _flat_entry(formats=[_audio_only()])
        with patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(entry)) as runner:
            await YoutubeResolver(cookies='# Netscape HTTP Cookie File\n').resolve(self._JOB)

        self.assertEqual('# Netscape HTTP Cookie File\n', runner.call_args.kwargs['cookies'])
        # 只取元数据，从不改成下载命令。
        self.assertEqual(
            ['--dump-json', '--no-playlist', 'https://www.youtube.com/watch?v=WrCjAAl9okA'],
            list(runner.call_args.args[0]),
        )

    async def test_a_height_limited_h264_track_is_picked_and_muxed_with_aac(self) -> None:
        entry = _flat_entry(
            formats=[
                _video(height=1080, url='https://googlevideo/1080'),
                _video(height=720, url='https://googlevideo/720'),
                _audio_only(url='https://googlevideo/aac'),
                _audio_only(acodec='opus', ext='webm', abr=160.0, url='https://googlevideo/opus'),
            ]
        )
        with patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(entry)):
            media = await YoutubeResolver().resolve(self._JOB)

        self.assertEqual('https://googlevideo/720', media.video_url)
        # 分轨的音轨必须是能合进 MP4 的 AAC，而不是码率更高的 opus。
        self.assertEqual('https://googlevideo/aac', media.audio_url)
        self.assertFalse(media.video_has_audio)

    async def test_a_progressive_mp4_with_aac_is_used_as_the_single_source(self) -> None:
        entry = _flat_entry(formats=[_video(acodec='mp4a.40.2', url='https://googlevideo/progressive.mp4')])
        with patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(entry)):
            media = await YoutubeResolver().resolve(self._JOB)

        self.assertEqual('https://googlevideo/progressive.mp4', media.video_url)
        self.assertTrue(media.video_has_audio)
        # keep_media=False 时仍拿这个渐进式文件去转写。
        self.assertEqual('https://googlevideo/progressive.mp4', media.audio_url)

    async def test_a_video_only_split_track_without_aac_falls_back_to_audio(self) -> None:
        # 没有 AAC 可合流：不给出一个合不成 MP4 的图轨，退回纯音频转写。
        entry = _flat_entry(
            formats=[
                _video(height=720, url='https://googlevideo/720'),
                _audio_only(acodec='opus', ext='webm', url='https://googlevideo/opus'),
            ]
        )
        with patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(entry)):
            media = await YoutubeResolver().resolve(self._JOB)

        self.assertIsNone(media.video_url)
        self.assertFalse(media.video_has_audio)
        self.assertEqual('https://googlevideo/opus', media.audio_url)

    async def test_a_progressive_video_with_opus_is_not_claimed_playable(self) -> None:
        # 渐进式里包着 opus，Hippo 不能原样当原生 MP4 播，不给 video_url。
        entry = _flat_entry(formats=[_video(acodec='opus', url='https://googlevideo/progressive.mp4')])
        with patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(entry)):
            media = await YoutubeResolver().resolve(self._JOB)

        self.assertIsNone(media.video_url)
        self.assertFalse(media.video_has_audio)
        self.assertEqual('https://googlevideo/progressive.mp4', media.audio_url)

    async def test_a_progressive_video_with_opus_uses_a_split_aac(self) -> None:
        # 能配上独立 AAC 时明确分轨（视频带声但需合流）。
        entry = _flat_entry(
            formats=[
                _video(acodec='opus', url='https://googlevideo/progressive.mp4'),
                _audio_only(),
            ]
        )
        with patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(entry)):
            media = await YoutubeResolver().resolve(self._JOB)

        self.assertEqual('https://googlevideo/progressive.mp4', media.video_url)
        self.assertFalse(media.video_has_audio)
        self.assertEqual('https://googlevideo/aac', media.audio_url)

    async def test_the_split_mux_skips_a_manifest_aac_and_takes_the_next_one(self) -> None:
        # 高码率的 AAC 是清单：不能遮住真正可下的低码率 AAC，也不能只因为“有这个格式”就当可合流。
        entry = _flat_entry(
            formats=[
                _video(height=720, url='https://googlevideo/720'),
                _audio_only(abr=160.0, protocol='https', url='https://manifest.googlevideo.com/api/index.m3u8'),
                _audio_only(abr=64.0, protocol='https', url='https://googlevideo/low.m4a'),
            ]
        )
        with patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(entry)):
            media = await YoutubeResolver().resolve(self._JOB)

        self.assertEqual('https://googlevideo/720', media.video_url)
        self.assertEqual('https://googlevideo/low.m4a', media.audio_url)
        self.assertFalse(media.video_has_audio)

    async def test_an_incompatible_video_keeps_the_original_audio(self) -> None:
        formats = [
            _video(height=1080, url='https://googlevideo/1080'),
            _video(vcodec='vp09.00.51.08', ext='webm', height=720, url='https://googlevideo/vp9'),
            _audio_only(),
        ]
        with patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(_flat_entry(formats=formats))):
            media = await YoutubeResolver().resolve(self._JOB)

        self.assertIsNone(media.video_url)
        self.assertFalse(media.video_has_audio)
        self.assertEqual('https://googlevideo/aac', media.audio_url)

    async def test_a_format_without_a_url_is_not_a_track(self) -> None:
        entry = _flat_entry(formats=[_video(url=''), _audio_only(url='')])
        with (
            patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(entry)),
            self.assertRaises(MediaJobSkipped),
        ):
            await YoutubeResolver().resolve(self._JOB)

    async def test_a_video_only_entry_without_any_audio_is_skipped(self) -> None:
        # 图轨没有声音，也配不到音轨：这样的条目转写不了，整条跳过。
        entry = _flat_entry(formats=[_video(height=720, url='https://googlevideo/720')])
        with (
            patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(entry)),
            self.assertRaises(MediaJobSkipped),
        ):
            await YoutubeResolver().resolve(self._JOB)

    async def test_the_embed_block_stays_metadata_only(self) -> None:
        entry = _flat_entry(formats=[_video(acodec='mp4a.40.2')])
        with patch('hippo.sources.youtube.run_ytdlp', new=_ytdlp(entry)):
            media = await YoutubeResolver().resolve(self._JOB)

        self.assertEqual('youtube', media.embed_block['provider'])
        self.assertEqual('WrCjAAl9okA', media.embed_block['video_id'])
        self.assertEqual(370, media.duration_seconds)


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


class _FakeProcess:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b'{}', b''


class YtdlpLocationTest(unittest.TestCase):
    """Where ``yt-dlp`` comes from, before it is executed."""

    def test_prefers_the_binary_on_path(self) -> None:
        with patch('hippo.sources.youtube.shutil.which', return_value='/usr/bin/yt-dlp'):
            self.assertEqual('/usr/bin/yt-dlp', ytdlp_bin())

    def test_falls_back_to_the_interpreter_environment(self) -> None:
        # systemd 启动的服务 PATH 里没有虚拟环境的 bin/，装在同一环境里的 yt-dlp
        # 只有靠这个回退才找得到。
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / 'yt-dlp'
            binary.write_text('#!/bin/sh', encoding='utf-8')
            binary.chmod(0o755)
            with (
                patch('hippo.sources.youtube.shutil.which', return_value=None),
                patch('hippo.sources.youtube.sys.executable', str(Path(tmp) / 'python')),
            ):
                self.assertEqual(str(binary), ytdlp_bin())

    def test_the_bare_name_is_kept_when_it_is_nowhere(self) -> None:
        # 保留裸名字：给用户的报错由 spawn 的 FileNotFoundError 给出。
        with (
            patch('hippo.sources.youtube.shutil.which', return_value=None),
            patch('hippo.sources.youtube.sys.executable', '/nowhere/python'),
        ):
            self.assertEqual('yt-dlp', ytdlp_bin())


if __name__ == '__main__':
    unittest.main()


class CookieFileTest(unittest.TestCase):
    """The submitted cookie reaches yt-dlp as a file it can read, and nowhere else."""

    def test_the_file_is_private_and_holds_the_cookie(self) -> None:
        cookies = '# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1800000000\tSID\tabc\n'

        path = write_cookie_file(cookies)

        self.assertIsNotNone(path)
        try:
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertEqual(cookies, path.read_text(encoding='utf-8'))
        finally:
            path.unlink()

    def test_a_blank_cookie_writes_nothing(self) -> None:
        self.assertIsNone(write_cookie_file(''))
        self.assertIsNone(write_cookie_file('   \n'))

    def test_a_cookie_without_a_trailing_newline_is_still_readable(self) -> None:
        # yt-dlp 不要求结尾换行，但补上更稳（有的解析器按行读）。
        path = write_cookie_file('SID=abc')

        try:
            self.assertEqual('SID=abc\n', path.read_text(encoding='utf-8'))
        finally:
            path.unlink()


class CookieFileRunTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_ytdlp_passes_the_cookie_file_and_removes_it(self) -> None:
        seen: dict[str, object] = {}

        async def fake_exec(*command, **kwargs):
            seen['command'] = command
            path = Path(command[command.index('--cookies') + 1])
            seen['exists'] = path.is_file()
            seen['content'] = path.read_text(encoding='utf-8')
            seen['mode'] = stat.S_IMODE(path.stat().st_mode)
            return _FakeProcess()

        with patch('hippo.sources.youtube.asyncio.create_subprocess_exec', new=fake_exec):
            await run_ytdlp(['--version'], cookies='# Netscape HTTP Cookie File\n')

        command = seen['command']
        self.assertIn('--cookies', command)
        self.assertTrue(seen['exists'])
        self.assertEqual(0o600, seen['mode'])
        # 跑完就删：临时目录里不留登录态。
        self.assertFalse(Path(command[command.index('--cookies') + 1]).exists())
