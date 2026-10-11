"""Bilibili source: URL parsing, the WBI signature, and the two ways to a transcript.

The signature test uses the worked example from the official API documentation, so
it fails loudly if the permutation or the encoding ever drifts.
"""

import unittest
from typing import ClassVar
from unittest.mock import AsyncMock

from hippo.media.pipeline import ResolvedMedia
from hippo.sources.bilibili import (
    BilibiliError,
    BilibiliResolver,
    BilibiliSource,
    best_audio_track,
    duration_from_length,
    embed_url,
    mixin_key,
    parse_ref,
    sign_wbi,
    subtitle_blocks,
    video_block,
)


class MixinKeyTest(unittest.TestCase):
    def test_the_documented_example_matches(self) -> None:
        # docs/misc/sign/wbi.md: img 7cd0849413… + sub 4932caff0f… → ea1db124af…
        key = mixin_key('7cd084941338484aae1ad9425b84077c', '4932caff0ff746eab6f01bf08b70ac45')

        self.assertEqual('ea1db124af3c7062474693fa704f4ff8', key)


class SignWbiTest(unittest.TestCase):
    def test_the_signature_follows_the_documented_algorithm(self) -> None:
        signed = sign_wbi(
            {'foo': '114', 'bar': '514', 'zab': 1919810},
            '7cd084941338484aae1ad9425b84077c',
            '4932caff0ff746eab6f01bf08b70ac45',
            wts=1702204169,
        )

        self.assertEqual('1702204169', signed['wts'])
        self.assertEqual('8f6f2b5b3d485fe1886cec6a0be8c5d4', signed['w_rid'])

    def test_forbidden_characters_are_stripped_from_values(self) -> None:
        signed = sign_wbi({'q': "a!b'c(d)e*f"}, 'k' * 32, 's' * 32, wts=1)

        self.assertNotIn('!', signed['q'])
        self.assertIn('abcdef', signed['q'])


class ParseRefTest(unittest.TestCase):
    def test_a_video_url_gives_its_bv_id(self) -> None:
        self.assertEqual(('video', 'BV1gHps6yEHL'), parse_ref('https://www.bilibili.com/video/BV1gHps6yEHL'))

    def test_a_space_url_gives_the_mid(self) -> None:
        self.assertEqual(('space', '946974'), parse_ref('https://space.bilibili.com/946974/video'))

    def test_a_foreign_host_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            parse_ref('https://evil.example/video/BV1gHps6yEHL')

    def test_a_multi_p_video_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            parse_ref('https://www.bilibili.com/video/BV1gHps6yEHL?p=2')

    def test_a_non_video_path_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            parse_ref('https://www.bilibili.com/bangumi/play/ep1')


class DurationTest(unittest.TestCase):
    def test_minutes_and_seconds(self) -> None:
        self.assertEqual(754, duration_from_length('12:34'))

    def test_hours(self) -> None:
        self.assertEqual(3723, duration_from_length('1:02:03'))

    def test_junk_is_none(self) -> None:
        self.assertIsNone(duration_from_length('unknown'))
        self.assertIsNone(duration_from_length(None))


class AudioTrackTest(unittest.TestCase):
    def test_the_highest_ordinary_bandwidth_wins(self) -> None:
        payload = {
            'data': {
                'dash': {
                    'audio': [
                        {'id': 30216, 'bandwidth': 65685, 'baseUrl': 'a'},
                        {'id': 30280, 'bandwidth': 175518, 'baseUrl': 'b'},
                        {'id': 30250, 'bandwidth': 999999, 'baseUrl': 'dolby'},
                    ]
                }
            }
        }

        self.assertEqual('b', best_audio_track(payload)['baseUrl'])

    def test_a_paid_only_reply_still_yields_a_track(self) -> None:
        payload = {'data': {'dash': {'audio': [{'id': 30250, 'bandwidth': 999999, 'baseUrl': 'dolby'}]}}}

        self.assertEqual('dolby', best_audio_track(payload)['baseUrl'])

    def test_no_dash_audio_is_none(self) -> None:
        self.assertIsNone(best_audio_track({'data': {'durl': [{'url': 'x'}]}}))


class SubtitleTest(unittest.TestCase):
    def test_entries_become_timestamped_paragraphs(self) -> None:
        blocks = subtitle_blocks(
            {'body': [{'from': 0.0, 'to': 1.0, 'content': '开场'}, {'from': 62.5, 'content': '正题'}]}
        )

        self.assertEqual(
            [{'type': 'paragraph', 'text': '开场', 't': 0.0}, {'type': 'paragraph', 'text': '正题', 't': 62.5}], blocks
        )

    def test_empty_entries_are_dropped(self) -> None:
        self.assertEqual([], subtitle_blocks({'body': [{'from': 0, 'content': '   '}]}))


class EmbedBlockTest(unittest.TestCase):
    def test_the_player_block_carries_the_embed_url(self) -> None:
        block = video_block('BV1x', cover_url='https://cdn/cover.jpg')

        self.assertEqual('video', block['type'])
        self.assertEqual(embed_url('BV1x'), block['embed_url'])
        self.assertIn('player.bilibili.com', block['embed_url'])
        self.assertEqual('https://cdn/cover.jpg', block['cover_url'])


class _FakeClient:
    def __init__(self, **responses):
        self._responses = responses
        for name in ('video', 'page_list', 'player', 'playurl', 'space_videos', 'space_info', 'fetch_json'):
            if not hasattr(self, name):
                setattr(self, name, AsyncMock(return_value=None))
        for name, value in responses.items():
            setattr(self, name, value if isinstance(value, AsyncMock) else AsyncMock(return_value=value))


class SourceListingTest(unittest.IsolatedAsyncioTestCase):
    async def test_listing_maps_the_vlist_into_items(self) -> None:
        client = _FakeClient(
            space_videos={
                'list': {
                    'vlist': [
                        {
                            'bvid': 'BV1a',
                            'title': '一期视频',
                            'pic': 'https://cdn/p.jpg',
                            'description': '简介',
                            'created': 1700000000,
                            'length': '10:05',
                            'author': '某UP',
                        }
                    ]
                },
                'page': {'count': 100},
            }
        )
        source = BilibiliSource(client)

        listing = await source.list_items(source_key='946974', limit=5)

        item = listing.items[0]
        self.assertEqual('BV1a', item.article_id)
        self.assertEqual('https://www.bilibili.com/video/BV1a', item.link)
        self.assertEqual(1700000000, item.publish_at)
        self.assertEqual(605, item.duration_seconds)
        # 100 条总量、第一页只取了 5 条 → 还有下一页
        self.assertFalse(listing.is_end)
        self.assertEqual('2', listing.next_cursor)

    async def test_the_last_page_ends_the_listing(self) -> None:
        client = _FakeClient(space_videos={'list': {'vlist': []}, 'page': {'count': 0}})

        listing = await BilibiliSource(client).list_items(source_key='1', limit=5)

        self.assertTrue(listing.is_end)
        self.assertIsNone(listing.next_cursor)

    async def test_a_risk_control_reply_is_the_listing_error(self) -> None:
        client = _FakeClient(space_videos=AsyncMock(side_effect=BilibiliError('B站风控拦截')))

        with self.assertRaises(BilibiliError) as ctx:
            await BilibiliSource(client).list_items(source_key='1', limit=5)

        self.assertIn('风控', str(ctx.exception))

    async def test_a_video_subscription_lists_that_one_video(self) -> None:
        # 订阅单个视频时 source_key 是 bvid，不是 mid：以前按空间查，B站 回 -400。
        client = _FakeClient(
            video={
                'bvid': 'BV1gHps6yEHL',
                'title': '影视飓风的直升机',
                'duration': 823,
                'pubdate': 1790000000,
                'pic': 'https://i0.hdslb.com/x.jpg',
                'desc': '简介',
                'owner': {'name': '影视飓风'},
                'aid': 123,
            }
        )
        source = BilibiliSource(client)

        listing = await source.list_items(source_key='BV1gHps6yEHL', limit=5)

        self.assertEqual(1, len(listing.items))
        self.assertTrue(listing.is_end)
        self.assertIsNone(listing.next_cursor)
        item = listing.items[0]
        self.assertEqual('BV1gHps6yEHL', item.article_id)
        self.assertEqual('影视飓风的直升机', item.title)
        self.assertEqual('https://www.bilibili.com/video/BV1gHps6yEHL', item.link)
        self.assertEqual(823, item.duration_seconds)
        self.assertEqual('影视飓风', item.author)
        self.assertEqual(1790000000, item.publish_at)
        # 空间列表接口不该被调用。
        client.space_videos.assert_not_awaited()

    async def test_a_video_subscription_that_is_gone_says_so(self) -> None:
        source = BilibiliSource(_FakeClient(video={}))

        with self.assertRaises(BilibiliError) as ctx:
            await source.list_items(source_key='BV1gHps6yEHL', limit=5)

        self.assertIn('不可见', str(ctx.exception))

    async def test_preview_of_a_space_falls_back_to_the_mid(self) -> None:
        client = _FakeClient(space_info=AsyncMock(side_effect=BilibiliError('风控')))

        info = await BilibiliSource(client).preview('https://space.bilibili.com/946974')

        self.assertEqual('bilibili:946974', info.biz)
        self.assertEqual('946974', info.nickname)


class ResolverTest(unittest.IsolatedAsyncioTestCase):
    _JOB: ClassVar[dict] = {'media_url': 'https://www.bilibili.com/video/BV1gHps6yEHL', 'source': 'bilibili'}

    async def test_the_audio_url_comes_back_with_the_headers_the_cdn_wants(self) -> None:
        client = _FakeClient(
            video={'bvid': 'BV1gHps6yEHL', 'pic': 'https://cdn/cover.jpg', 'duration': 800, 'pages': [{'cid': 42}]},
            player={'subtitle': {'subtitles': []}},
            playurl={'dash': {'audio': [{'id': 30280, 'bandwidth': 175518, 'baseUrl': 'https://cdn/audio.m4s'}]}},
        )

        media = await BilibiliResolver(client).resolve(self._JOB)

        self.assertIsInstance(media, ResolvedMedia)
        self.assertEqual('https://cdn/audio.m4s', media.audio_url)
        self.assertIn('Referer', media.headers)
        self.assertEqual(800, media.duration_seconds)
        self.assertEqual('BV1gHps6yEHL', media.embed_block['video_id'])

    async def test_a_cc_subtitle_still_carries_the_audio_url(self) -> None:
        # CC 只省 ASR，不省留档：音频地址与 CDN 需要的 Referer 一起带出来。
        client = _FakeClient(
            video={'bvid': 'BV1gHps6yEHL', 'duration': 800, 'pages': [{'cid': 42}]},
            player={'subtitle': {'subtitles': [{'subtitle_url': '//cdn/sub.json'}]}},
            fetch_json={'body': [{'from': 0.0, 'content': '字幕'}]},
            playurl={'dash': {'audio': [{'id': 30216, 'bandwidth': 65685, 'baseUrl': 'https://cdn/a.m4s'}]}},
        )

        media = await BilibiliResolver(client).resolve(self._JOB)

        self.assertEqual('https://cdn/a.m4s', media.audio_url)
        self.assertIn('Referer', media.headers)
        self.assertEqual('字幕', media.subtitle.blocks[0]['text'])
        self.assertEqual('字幕', media.subtitle.markdown)

    async def test_a_cc_subtitle_survives_a_failing_playurl(self) -> None:
        # 音频接口挂了也不该把已经拿到的 CC 字幕一起丢掉。
        client = _FakeClient(
            video={'bvid': 'BV1gHps6yEHL', 'duration': 800, 'pages': [{'cid': 42}]},
            player={'subtitle': {'subtitles': [{'subtitle_url': '//cdn/sub.json'}]}},
            fetch_json={'body': [{'from': 0.0, 'content': '字幕'}]},
            playurl={},
        )

        media = await BilibiliResolver(client).resolve(self._JOB)

        self.assertIsNone(media.audio_url)
        self.assertEqual('字幕', media.subtitle.blocks[0]['text'])

    async def test_cc_can_be_turned_off(self) -> None:
        client = _FakeClient(
            video={'bvid': 'BV1gHps6yEHL', 'pages': [{'cid': 42}]},
            player={'subtitle': {'subtitles': [{'subtitle_url': '//cdn/sub.json'}]}},
            fetch_json={'body': [{'from': 0.0, 'content': '字幕'}]},
            playurl={'dash': {'audio': [{'id': 30216, 'bandwidth': 65685, 'baseUrl': 'https://cdn/a.m4s'}]}},
        )

        media = await BilibiliResolver(client, use_cc_subtitle=False).resolve(self._JOB)

        self.assertEqual('https://cdn/a.m4s', media.audio_url)

    async def test_force_asr_skips_a_cc_subtitle(self) -> None:
        client = _FakeClient(
            video={'bvid': 'BV1gHps6yEHL', 'pages': [{'cid': 42}]},
            player={'subtitle': {'subtitles': [{'subtitle_url': '//cdn/sub.json'}]}},
            fetch_json={'body': [{'from': 0.0, 'content': '字幕'}]},
            playurl={'dash': {'audio': [{'id': 30280, 'bandwidth': 1, 'baseUrl': 'https://cdn/a.m4s'}]}},
        )
        job = {**self._JOB, 'payload': {'skip_subtitle': True}}

        media = await BilibiliResolver(client).resolve(job)

        self.assertEqual('https://cdn/a.m4s', media.audio_url)
        self.assertIsNone(media.subtitle)

    async def test_a_video_without_a_page_is_skipped(self) -> None:
        from hippo.media.pipeline import MediaJobSkipped

        client = _FakeClient(video={})

        with self.assertRaises(MediaJobSkipped):
            await BilibiliResolver(client).resolve(self._JOB)


if __name__ == '__main__':
    unittest.main()
