"""Podcast source: feed parsing, duration formats, and the transcript-first path.

The feed fixtures mirror what real feeds actually contain — several namespaces,
CDATA, both duration spellings, an item without an enclosure, and the
Podcasting 2.0 transcript tag — because that variety is the whole difficulty. A
summary that arrives as markup is fixture coverage of its own: the digest the
reader's card and the RSS export show has to come out as text.
"""

import unittest
from unittest.mock import AsyncMock, patch

from defusedxml.common import DefusedXmlException

from hippo.media.pipeline import MediaJobSkipped
from hippo.normalize_html import normalize_html
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

TEXT_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>阮一峰的网络日志</title>
  <entry>
    <title>科技爱好者周刊</title>
    <id>tag:blog,2026:414</id>
    <link rel="alternate" href="https://blog.example/414"/>
    <updated>2026-10-11T11:41:55Z</updated>
    <content type="html">&lt;p&gt;每周分享科技内容。&lt;/p&gt;</content>
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

#: The same description field in every shape a real feed ships it: CDATA HTML,
#: escaped HTML, an escaped XML wrapper, and plain text with entities.
CDATA_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Feed A</title>
    <link>https://cdata.example</link>
    <item>
      <title>cdata</title>
      <guid>cdata-1</guid>
      <description><![CDATA[<p>first &amp; second</p><p>next paragraph</p><script>bad()</script><style>.x{display:none}</style>]]></description>
      <enclosure url="https://cdn.example/cdata.mp3" type="audio/mpeg"/>
    </item>
    <item>
      <title>escaped</title>
      <guid>cdata-2</guid>
      <description>&lt;p&gt;escaped line&lt;/p&gt;&lt;script&gt;bad()&lt;/script&gt;</description>
      <enclosure url="https://cdn.example/cdata2.mp3" type="audio/mpeg"/>
    </item>
    <item>
      <title>wrapped</title>
      <guid>cdata-3</guid>
      <description>&lt;article&gt;&lt;body&gt;&lt;p&gt;wrapped text&lt;/p&gt;&lt;/body&gt;&lt;/article&gt;</description>
      <enclosure url="https://cdn.example/cdata3.mp3" type="audio/mpeg"/>
    </item>
    <item>
      <title>plain</title>
      <guid>cdata-4</guid>
      <description>5 &lt; 6 and 7 &gt; 4, AT&amp;T</description>
      <enclosure url="https://cdn.example/cdata4.mp3" type="audio/mpeg"/>
    </item>
  </channel>
</rss>
"""

#: A text feed: one item with both a summary and a body, one with only a body.
CONTENT_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Feed B</title>
    <link>https://blog.example</link>
    <item>
      <title>summary and body</title>
      <guid>post-1</guid>
      <link>https://blog.example/post-1</link>
      <description><![CDATA[<p>summary &amp; more</p>]]></description>
      <content:encoded><![CDATA[<h2>subhead</h2><p>body one</p><p>body two</p>]]></content:encoded>
    </item>
    <item>
      <title>body only</title>
      <guid>post-2</guid>
      <link>https://blog.example/post-2</link>
      <content:encoded><![CDATA[<p>only content</p><p>second line</p><script>bad()</script>]]></content:encoded>
    </item>
  </channel>
</rss>
"""

#: Atom spells the summary ``<summary>``, in either markup type, and may leave it empty.
ATOM_SUMMARY = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom summaries</title>
  <entry>
    <title>html summary</title>
    <id>tag:atom,2026:summary-html</id>
    <link rel="alternate" href="https://atom.example/summary-html"/>
    <summary type="html">&lt;p&gt;one &amp; summary&lt;/p&gt;</summary>
  </entry>
  <entry>
    <title>xhtml summary</title>
    <id>tag:atom,2026:summary-xhtml</id>
    <link rel="alternate" href="https://atom.example/summary-xhtml"/>
    <summary type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>structured summary</p><p>second line</p></div></summary>
  </entry>
  <entry>
    <title>type-less summary</title>
    <id>tag:atom,2026:summary-default</id>
    <link rel="alternate" href="https://atom.example/summary-default"/>
    <summary>a &lt;b&gt; c</summary>
  </entry>
  <entry>
    <title>empty summary</title>
    <id>tag:atom,2026:summary-empty</id>
    <link rel="alternate" href="https://atom.example/summary-empty"/>
    <summary type="text"> </summary>
  </entry>
</feed>
"""

#: Atom bodies: ``type="html"`` arrives escaped, ``xhtml`` as real child nodes, and
#: the type-less default is literal text — which is exactly where the markup lives
#: that must not be parsed. The RSS body below has no type either, and there markup
#: is the convention, so the same shape means text in one document and markup in the
#: other.
ATOM_BODY = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom bodies</title>
  <entry>
    <title>xhtml body</title>
    <id>tag:atom,2026:body-xhtml</id>
    <link rel="alternate" href="https://atom.example/body-xhtml"/>
    <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>first line</p><p>second line</p></div></content>
  </entry>
  <entry>
    <title>html body</title>
    <id>tag:atom,2026:body-html</id>
    <link rel="alternate" href="https://atom.example/body-html"/>
    <content type="html">&lt;p&gt;body &amp; text&lt;/p&gt;</content>
  </entry>
  <entry>
    <title>type-less body</title>
    <id>tag:atom,2026:body-text</id>
    <link rel="alternate" href="https://atom.example/body-text"/>
    <content>a &lt;b&gt; c &amp; d</content>
  </entry>
</feed>
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


class SummaryExtractionTest(unittest.TestCase):
    """The digest is text and the body keeps its structure.

    A feed ships the same summary as CDATA HTML, escaped HTML, an escaped XML
    wrapper or plain text; all four used to reach the digest — and from there the
    RSS export — with their tags intact.
    """

    def test_a_cdata_html_description_becomes_text(self) -> None:
        episode = parse_feed(CDATA_FEED).episodes[0]

        self.assertEqual('first & second\nnext paragraph', episode.description)

    def test_an_escaped_html_description_loses_its_tags(self) -> None:
        episode = parse_feed(CDATA_FEED).episodes[1]

        self.assertEqual('escaped line', episode.description)

    def test_an_escaped_xml_wrapper_is_not_visible_text(self) -> None:
        episode = parse_feed(CDATA_FEED).episodes[2]

        self.assertEqual('wrapped text', episode.description)
        for wrapper in ('<article', '</article>', '<body', '</body>'):
            self.assertNotIn(wrapper, episode.description)

    def test_plain_text_entities_are_decoded_once(self) -> None:
        episode = parse_feed(CDATA_FEED).episodes[3]

        self.assertEqual('5 < 6 and 7 > 4, AT&T', episode.description)

    def test_script_and_style_never_reach_the_digest(self) -> None:
        feed = parse_feed(CDATA_FEED)

        for episode in feed.episodes:
            self.assertNotIn('bad()', episode.description or '')
            self.assertNotIn('display:none', episode.description or '')

    def test_an_atom_html_summary_is_read(self) -> None:
        episode = parse_feed(ATOM_SUMMARY).episodes[0]

        self.assertEqual('one & summary', episode.description)

    def test_an_atom_xhtml_summary_keeps_its_paragraphs(self) -> None:
        episode = parse_feed(ATOM_SUMMARY).episodes[1]

        self.assertEqual('structured summary\nsecond line', episode.description)

    def test_a_type_less_atom_summary_is_literal_text(self) -> None:
        # Atom's default is text: what looks like a tag was written by the author and
        # must survive as written instead of being parsed away.
        episode = parse_feed(ATOM_SUMMARY).episodes[2]

        self.assertEqual('a <b> c', episode.description)

    def test_an_empty_atom_summary_is_no_summary(self) -> None:
        episode = parse_feed(ATOM_SUMMARY).episodes[3]

        self.assertIsNone(episode.description)

    def test_a_summary_does_not_replace_the_body(self) -> None:
        episode = parse_feed(CONTENT_FEED).episodes[0]

        self.assertEqual('summary & more', episode.description)
        self.assertIn('<h2>subhead</h2>', episode.body_html)
        self.assertIn('<p>body two</p>', episode.body_html)

    def test_a_missing_summary_falls_back_to_the_body_text(self) -> None:
        episode = parse_feed(CONTENT_FEED).episodes[1]

        self.assertEqual('only content\nsecond line', episode.description)
        # The body stays complete after a summary was derived from it.
        self.assertIn('<p>second line</p>', episode.body_html)
        self.assertNotIn('bad()', episode.description)

    def test_an_html_atom_body_is_read_as_markup(self) -> None:
        episode = parse_feed(ATOM_BODY).episodes[1]

        self.assertEqual('body & text', episode.description)
        self.assertIn('<p>body & text</p>', episode.body_html)

    def test_an_xhtml_atom_body_keeps_paragraph_structure(self) -> None:
        episode = parse_feed(ATOM_BODY).episodes[0]

        self.assertEqual('first line\nsecond line', episode.description)
        body = episode.body_html or ''
        self.assertLess(body.index('<p>first line</p>'), body.index('<p>second line</p>'))
        # The feed's own <content> wrapper and namespace are not part of the body.
        self.assertNotIn('<content', body)
        self.assertNotIn('Atom', body)

    def test_an_xhtml_atom_body_still_normalizes(self) -> None:
        episode = parse_feed(ATOM_BODY).episodes[0]

        markdown = normalize_html(episode.body_html, 'markdown')

        self.assertIn('first line', markdown)
        self.assertIn('second line', markdown)
        self.assertNotIn('xmlns', markdown)

    def test_a_type_less_atom_body_keeps_its_angle_brackets(self) -> None:
        # The same shape in RSS means markup, so the default has to be decided by the
        # document's namespace rather than by the element alone.
        episode = parse_feed(ATOM_BODY).episodes[2]

        self.assertEqual('a <b> c & d', episode.description)
        self.assertIn('a &lt;b&gt; c &amp; d', episode.body_html)

    def test_a_type_less_rss_body_is_still_markup(self) -> None:
        # No type and no namespace: RSS's convention applies, so the angle brackets
        # are markup and stay unescaped in the body the reader renders.
        feed = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
            '<channel><title>r</title><item><title>i</title><guid>g</guid>'
            '<content:encoded>a &lt;b&gt; c</content:encoded></item></channel></rss>'
        )

        episode = parse_feed(feed).episodes[0]

        self.assertIn('<b>', episode.body_html or '')

    def test_a_deeply_nested_xhtml_body_does_not_blow_the_stack(self) -> None:
        # A feed can nest its body hundreds of levels deep; serializing it has to
        # stop rather than run into the recursion limit.
        depth = 200
        body = '<div>' * depth + 'deep text' + '</div>' * depth
        feed = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom"><title>deep</title><entry>'
            '<title>deep</title><id>tag:atom,2026:deep</id>'
            f'<content type="xhtml">{body}</content></entry></feed>'
        )

        episode = parse_feed(feed).episodes[0]

        self.assertIn('deep text', episode.description)
        self.assertNotIn('</div>' * depth, episode.body_html or '')


class GzipLimitTest(unittest.TestCase):
    """A compressed feed is read with a ceiling: a bomb must not expand first."""

    def test_a_gzipped_feed_is_unpacked(self) -> None:
        import gzip

        feed = (
            '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>x</title>'
            '<item><title>i</title><guid>g</guid></item></channel></rss>'
        )

        self.assertEqual(1, len(parse_feed(gzip.compress(feed.encode('utf-8'))).episodes))

    def test_a_gzip_bomb_is_refused_before_it_expands(self) -> None:
        import gzip

        # A few KB compressed, far past the limit once expanded: the ceiling applies
        # while reading, not after a full expansion.
        bomb = gzip.compress(b'a' * (64 * 1024))
        with (
            patch('hippo.sources.podcast.MAX_FEED_BYTES', 4096),
            self.assertRaises(PodcastError) as ctx,
        ):
            parse_feed(bomb)

        self.assertIn('上限', str(ctx.exception))


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
        # 有音频附件 → 播客
        self.assertEqual('podcast', info.source)

    async def test_preview_calls_a_text_feed_a_feed(self) -> None:
        # 只有正文、没有音频附件的 feed（博客/周刊）：同一套解析，不同的来源名，
        # 因为它不该被当成播客展示，也没有音频可转写。
        client = _client(feed=AsyncMock(return_value=parse_feed(TEXT_FEED)))

        info = await PodcastSource(client).preview('https://blog.example/atom.xml')

        self.assertEqual('feed', info.source)
        self.assertEqual('阮一峰的网络日志', info.nickname)

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
        # 卡片和 RSS 导出读的是 digest：CDATA HTML 在这里必须是纯文本
        self.assertEqual('第一期简介', first.digest)

    async def test_a_text_feed_keeps_the_body_and_summarizes_it(self) -> None:
        client = _client(feed=AsyncMock(return_value=parse_feed(CONTENT_FEED)))

        listing = await PodcastSource(client).list_items(source_key='https://blog.example/rss', limit=5)

        first, second = listing.items
        self.assertEqual('summary & more', first.digest)
        self.assertIn('<h2>subhead</h2>', first.content_html or '')
        self.assertEqual('only content\nsecond line', second.digest)
        self.assertIn('only content', second.content_html or '')
        # 有正文就不排转写任务（原来就没有音频）
        self.assertEqual('', second.media_url)

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

        # 现成转录稿只省掉 ASR：音频地址仍然带出来，让 pipeline 能照常留档。
        self.assertEqual('https://cdn.example/ep1.mp3', media.audio_url)
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

    async def test_force_asr_ignores_a_published_transcript(self) -> None:
        transcript = AsyncMock(return_value=VTT)
        client = _client(transcript=transcript)
        job = {
            'media_url': 'https://cdn.example/ep1.mp3',
            'payload': {'transcript_url': 'https://x.vtt', 'skip_subtitle': True},
        }

        media = await PodcastResolver(client).resolve(job)

        self.assertEqual('https://cdn.example/ep1.mp3', media.audio_url)
        transcript.assert_not_awaited()

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
