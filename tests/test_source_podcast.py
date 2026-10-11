"""Podcast source: feed parsing, duration formats, and the transcript-first path.

The feed fixtures mirror what real feeds actually contain — several namespaces,
CDATA, both duration spellings, an item without an enclosure, and the
Podcasting 2.0 transcript tag — because that variety is the whole difficulty.
"""

import unittest
from unittest.mock import AsyncMock

from defusedxml.common import DefusedXmlException

from hippo.media.pipeline import MediaJobSkipped
from hippo.sources.podcast import (
    PodcastClient,
    PodcastError,
    PodcastResolver,
    PodcastSource,
    feed_biz,
    normalize_feed_url,
    parse_duration,
    parse_feed,
    parse_published,
)

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>一档播客</title>
    <link>https://podcast.example</link>
    <itunes:image href="https://cdn.example/cover.jpg"/>
    <item>
      <title>第一期</title>
      <guid isPermaLink="false">ep-1</guid>
      <link>https://podcast.example/ep1</link>
      <pubDate>Tue, 06 Oct 2026 07:00:00 -0000</pubDate>
      <itunes:duration>59:17</itunes:duration>
      <description><![CDATA[<p>第一期简介</p>]]></description>
      <enclosure url="https://cdn.example/ep1.mp3" type="audio/mpeg" length="1000"/>
      <podcast:transcript url="https://cdn.example/ep1.vtt" type="text/vtt"/>
    </item>
    <item>
      <title>第二期（无音频）</title>
      <guid>ep-2</guid>
      <pubDate>2026-10-05T00:00:00Z</pubDate>
      <itunes:duration>3600</itunes:duration>
      <content:encoded><![CDATA[<p>文字稿</p>]]></content:encoded>
    </item>
  </channel>
</rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <title>Atom 播客</title>
  <link rel="alternate" href="https://atom.example"/>
  <itunes:image href="https://cdn.example/atom.jpg"/>
  <entry>
    <title>Atom 一期</title>
    <id>tag:atom,2026:1</id>
    <link rel="alternate" href="https://atom.example/1"/>
    <published>2026-10-04T12:00:00+00:00</published>
    <itunes:duration>00:26:28</itunes:duration>
    <link rel="enclosure" type="audio/mpeg" href="https://cdn.example/atom1.mp3"/>
  </entry>
</feed>
"""

VTT = """WEBVTT

00:00:00.000 --> 00:00:04.000
开场白，大家好。

00:01:02.500 --> 00:01:06.000
<i>第二段</i>正题开始。
"""

SRT = """1
00:00:01,000 --> 00:00:03,000
第一句

2
00:00:04,000 --> 00:00:06,000
第二句
"""


class FeedUrlTest(unittest.TestCase):
    def test_a_plain_http_url_is_kept(self) -> None:
        self.assertEqual('https://feed.example/rss', normalize_feed_url(' https://feed.example/rss '))

    def test_something_that_is_not_a_url_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            normalize_feed_url('feed.example/rss')

    def test_the_biz_is_stable_and_url_specific(self) -> None:
        first = feed_biz('https://feed.example/rss')

        self.assertEqual(first, feed_biz('https://feed.example/rss'))
        self.assertNotEqual(first, feed_biz('https://feed.example/other'))
        self.assertTrue(first.startswith('podcast:'))


class DurationTest(unittest.TestCase):
    def test_plain_seconds(self) -> None:
        self.assertEqual(3600, parse_duration('3600'))

    def test_minutes_and_seconds(self) -> None:
        self.assertEqual(3557, parse_duration('59:17'))

    def test_hours_minutes_seconds(self) -> None:
        self.assertEqual(1588, parse_duration('00:26:28'))
        self.assertEqual(19317, parse_duration('5:21:57'))

    def test_junk_is_none(self) -> None:
        self.assertIsNone(parse_duration('unknown'))
        self.assertIsNone(parse_duration(''))


class PublishedTest(unittest.TestCase):
    def test_rfc822_with_an_odd_zone(self) -> None:
        # "-0000" 表示「时间是 UTC 但本机时区未知」，stdlib 会返回 naive datetime，
        # 必须当 UTC 处理，否则会在服务器本地时区上漂 8 小时。
        self.assertEqual(1791270000, parse_published('Tue, 06 Oct 2026 07:00:00 -0000'))

    def test_iso8601(self) -> None:
        self.assertEqual(1791158400, parse_published('2026-10-05T00:00:00Z'))

    def test_junk_is_none(self) -> None:
        self.assertIsNone(parse_published('not a date'))


class ParseFeedTest(unittest.TestCase):
    def test_an_rss_feed_with_everything(self) -> None:
        feed = parse_feed(RSS)

        self.assertEqual('一档播客', feed.title)
        self.assertEqual('https://cdn.example/cover.jpg', feed.image_url)
        self.assertEqual(2, len(feed.episodes))
        first, second = feed.episodes
        self.assertEqual('ep-1', first.guid)
        self.assertEqual('https://cdn.example/ep1.mp3', first.audio_url)
        self.assertEqual('audio/mpeg', first.audio_type)
        self.assertEqual(3557, first.duration_seconds)
        self.assertEqual('https://cdn.example/ep1.vtt', first.transcript_url)
        self.assertEqual('text/vtt', first.transcript_type)
        self.assertIsNotNone(first.published_at)
        # 没有 enclosure 的条目照样进得来：它会退化成一篇文字文章
        self.assertIsNone(second.audio_url)
        self.assertIn('文字稿', second.body_html or '')

    def test_an_atom_feed(self) -> None:
        feed = parse_feed(ATOM)

        self.assertEqual('Atom 播客', feed.title)
        self.assertEqual(1, len(feed.episodes))
        episode = feed.episodes[0]
        self.assertEqual('tag:atom,2026:1', episode.guid)
        self.assertEqual('https://cdn.example/atom1.mp3', episode.audio_url)
        self.assertEqual(1588, episode.duration_seconds)

    def test_a_gzipped_feed_is_unpacked(self) -> None:
        import gzip

        feed = parse_feed(gzip.compress(RSS.encode('utf-8')))

        self.assertEqual(2, len(feed.episodes))

    def test_something_that_is_not_a_feed_is_an_error(self) -> None:
        with self.assertRaises(PodcastError):
            parse_feed(b'<html><body>not a feed</body></html>')

    def test_malformed_xml_is_an_error(self) -> None:
        with self.assertRaises(PodcastError):
            parse_feed(b'<rss><channel><title>x</title>')

    def test_an_entity_bomb_is_refused(self) -> None:
        # defusedxml 的存在理由：feed 是用户给的任意 XML
        bomb = (
            '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
            '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">]>'
            '<rss><channel><title>&lol2;</title></channel></rss>'
        )

        # defusedxml 会在这里抛 EntitiesForbidden（DefusedXmlException 的子类）
        with self.assertRaises(DefusedXmlException):
            parse_feed(bomb.encode('utf-8'))

    def test_an_empty_feed_is_an_error(self) -> None:
        with self.assertRaises(PodcastError):
            parse_feed(b'<rss><channel><title>x</title></channel></rss>')


def _client(**overrides) -> PodcastClient:
    client = PodcastClient.__new__(PodcastClient)
    client._client = None  # type: ignore[assignment]
    client._owns_client = False
    for name, value in overrides.items():
        setattr(client, name, value)
    return client


class SourceTest(unittest.IsolatedAsyncioTestCase):
    async def test_preview_reads_the_channel_title(self) -> None:
        client = _client(feed=AsyncMock(return_value=parse_feed(RSS)))

        info = await PodcastSource(client).preview('https://feed.example/rss')

        self.assertEqual('一档播客', info.nickname)
        self.assertEqual('https://feed.example/rss', info.source_key)
        self.assertEqual(feed_biz('https://feed.example/rss'), info.biz)

    async def test_listing_maps_episodes_and_keeps_the_transcript_url(self) -> None:
        client = _client(feed=AsyncMock(return_value=parse_feed(RSS)))

        listing = await PodcastSource(client).list_items(source_key='https://feed.example/rss', limit=5)

        self.assertEqual(2, len(listing.items))
        first = listing.items[0]
        self.assertEqual('ep-1', first.article_id)
        self.assertEqual('https://cdn.example/ep1.mp3', first.media_url)
        self.assertEqual(3557, first.duration_seconds)
        # 转录稿地址跟着任务走，解析阶段不必再拉一遍 feed
        self.assertEqual('https://cdn.example/ep1.vtt', first.raw['transcript_url'])

    async def test_a_window_larger_than_the_feed_ends_the_listing(self) -> None:
        client = _client(feed=AsyncMock(return_value=parse_feed(RSS)))

        listing = await PodcastSource(client).list_items(source_key='f', limit=1)

        self.assertFalse(listing.is_end)
        self.assertEqual('1', listing.next_cursor)


class ResolverTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_published_transcript_beats_the_asr(self) -> None:
        client = _client(transcript=AsyncMock(return_value=VTT))
        job = {
            'media_url': 'https://cdn.example/ep1.mp3',
            'source': 'podcast',
            'payload': {'transcript_url': 'https://cdn.example/ep1.vtt', 'duration_seconds': 3557},
        }

        media = await PodcastResolver(client).resolve(job)

        self.assertIsNone(media.audio_url)
        self.assertEqual(3557, media.duration_seconds)
        # 短句会被并成一段（合并是设计的一部分），锚点用该段首句的真实时间
        self.assertTrue(media.subtitle.blocks[0]['text'].startswith('开场白'))
        self.assertEqual(0.0, media.subtitle.blocks[0]['t'])
        self.assertIn('第二段', media.subtitle.markdown)

    async def test_an_srt_transcript_also_works(self) -> None:
        client = _client(transcript=AsyncMock(return_value=SRT))
        job = {'media_url': 'https://cdn.example/ep.mp3', 'payload': {'transcript_url': 'https://x.srt'}}

        media = await PodcastResolver(client).resolve(job)

        self.assertTrue(media.subtitle.blocks[0]['text'].startswith('第一句'))

    async def test_a_broken_transcript_falls_back_to_the_audio(self) -> None:
        client = _client(transcript=AsyncMock(side_effect=PodcastError('404')))
        job = {'media_url': 'https://cdn.example/ep1.mp3', 'payload': {'transcript_url': 'https://x.vtt'}}

        media = await PodcastResolver(client).resolve(job)

        self.assertEqual('https://cdn.example/ep1.mp3', media.audio_url)

    async def test_an_empty_transcript_falls_back_to_the_audio(self) -> None:
        client = _client(transcript=AsyncMock(return_value='WEBVTT\n\n'))
        job = {'media_url': 'https://cdn.example/ep1.mp3', 'payload': {'transcript_url': 'https://x.vtt'}}

        media = await PodcastResolver(client).resolve(job)

        self.assertEqual('https://cdn.example/ep1.mp3', media.audio_url)

    async def test_no_audio_and_no_transcript_is_a_skip(self) -> None:
        client = _client(transcript=AsyncMock(return_value=VTT))

        with self.assertRaises(MediaJobSkipped):
            await PodcastResolver(client).resolve({'media_url': '', 'payload': {}})

    async def test_the_proxy_travels_with_the_audio(self) -> None:
        client = _client(transcript=AsyncMock(return_value=VTT))

        media = await PodcastResolver(client, proxy='http://proxy:8082').resolve({'media_url': 'https://x.mp3'})

        self.assertEqual('http://proxy:8082', media.proxy)

    async def test_a_job_without_a_payload_still_uses_the_audio(self) -> None:
        client = _client(transcript=AsyncMock(return_value=VTT))

        media = await PodcastResolver(client).resolve({'media_url': 'https://x.mp3'})

        self.assertEqual('https://x.mp3', media.audio_url)


if __name__ == '__main__':
    unittest.main()
