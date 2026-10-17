"""RSS/Atom podcasts.

Two things make this source different from the video ones: the audio URL is in the
feed (so nothing has to be resolved), and roughly half the episodes publish a
transcript — which is why the transcript is checked *before* the ASR ever runs.

The feed is untrusted input from an arbitrary URL, so it is parsed with
``defusedxml``: a feed can otherwise expand an entity bomb in the middle of a
20 MB download. The download itself is capped for the same reason (real feeds do
reach 20 MB with a few thousand episodes).
"""

from __future__ import annotations

import gzip
import hashlib
import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import httpx
from bs4 import BeautifulSoup
from defusedxml import ElementTree

from ..logger import get_logger
from ..media.pipeline import MediaJobSkipped, ResolvedMedia, Subtitle
from ..media.subtitle import blocks as subtitle_blocks
from ..media.subtitle import markdown as subtitle_markdown
from ..media.subtitle import parse_timed_text
from . import SourceError
from .ingest import ChannelInfo, Item, Listing

logger = get_logger(__name__)

SOURCE = 'podcast'
#: A feed whose entries carry an enclosure is a podcast; one whose entries are text is
#: a blog. Same parser and same adapter — only the label and the badge differ, and the
#: parse is the only place that can tell.
FEED_SOURCE = 'feed'

#: Real feeds can be large (The Daily's is ~20 MB) but not unbounded; this is a
#: memory guard, not a policy.
MAX_FEED_BYTES = 24 * 1024 * 1024
#: A transcript file, by contrast, is a few hundred KB.
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024

FEED_TIMEOUT = 120.0
TRANSCRIPT_TIMEOUT = 60.0

_NS = {
    'atom': 'http://www.w3.org/2005/Atom',
    'content': 'http://purl.org/rss/1.0/modules/content/',
    'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd',
    'media': 'http://search.yahoo.com/mrss/',
    'podcast': 'https://podcastindex.org/namespace/1.0',
    # 少数 feed 还在用 podcastindex 的旧命名空间
    'podcast-legacy': 'https://github.com/Podcastindex-org/podcast-namespace/blob/main/docs/1.0.md',
}

_TRANSCRIPT_TYPES = ('text/vtt', 'application/srt', 'application/x-subrip', 'text/plain', 'text/srt')


class PodcastError(SourceError):
    pass


@dataclass(frozen=True, slots=True)
class Episode:
    guid: str
    title: str
    link: str
    audio_url: str | None = None
    audio_type: str | None = None
    published_at: int | None = None
    duration_seconds: int | None = None
    image_url: str | None = None
    description: str | None = None
    transcript_url: str | None = None
    transcript_type: str | None = None
    body_html: str | None = None


@dataclass(frozen=True, slots=True)
class Feed:
    title: str
    link: str
    image_url: str | None = None
    episodes: list[Episode] = field(default_factory=list)


def feed_biz(feed_url: str, kind: str = SOURCE) -> str:
    """A stable ``biz`` for a feed URL, since the URL itself is long and mutable.

    The prefix is the kind the preview resolved (``podcast`` or ``feed``) and the
    account stores that same value: the two disagreeing is what makes an account
    impossible to rename later, because the biz *is* its identity.
    """
    digest = hashlib.sha1(feed_url.strip().encode('utf-8')).hexdigest()
    return f'{kind}:{digest[:16]}'


def normalize_feed_url(value: str) -> str:
    url = (value or '').strip()
    if not url.startswith(('http://', 'https://')):
        raise ValueError('请输入完整的 http(s) 订阅地址')
    return url


def parse_duration(value: Any) -> int | None:
    """``itunes:duration`` comes as seconds, ``MM:SS`` or ``HH:MM:SS``.

    All three appear in the wild in the same feed, so none of them can be assumed.
    """
    text = str(value or '').strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    parts = text.split(':')
    if not all(part.strip().isdigit() for part in parts) or len(parts) > 3:
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def parse_published(value: Any) -> int | None:
    """RSS ``pubDate`` is RFC822 with several real-world spellings; Atom is ISO8601."""
    text = str(value or '').strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except TypeError, ValueError:
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _text(element: Any) -> str:
    return ''.join(element.itertext()).strip() if element is not None else ''


#: Elements a feed puts in a text field that a reader never sees. Script and style
#: matter most: their contents would otherwise land in the digest as visible text.
_INVISIBLE_TAGS = ('script', 'style', 'noscript', 'template', 'head', 'title', 'meta', 'link')
#: Block boundaries become line breaks, so two paragraphs do not read as one sentence.
_BLOCK_TAGS = (
    'p',
    'div',
    'br',
    'hr',
    'li',
    'ul',
    'ol',
    'h1',
    'h2',
    'h3',
    'h4',
    'h5',
    'h6',
    'blockquote',
    'pre',
    'table',
    'tr',
    'td',
    'th',
    'section',
    'article',
    'figure',
    'figcaption',
)
#: A feed that nests its body thousands of levels deep does so to blow the recursion
#: limit; past this the subtree is flattened back to text instead.
_MAX_MARKUP_DEPTH = 50
_VOID_TAGS = ('br', 'hr', 'img', 'input')


def _lines(text: str) -> str:
    """Whitespace-normalized lines, blank lines dropped."""
    return '\n'.join(line for line in (' '.join(raw.split()) for raw in (text or '').splitlines()) if line)


def _visible_text(fragment: str) -> str:
    """Plain text of a markup fragment: entities decoded once, paragraphs kept.

    BeautifulSoup parses but never fetches anything, and one parse cannot loop the
    way repeated unescaping would. Scripts, styles and the other non-rendered
    elements go, and block boundaries become line breaks. Fragments without markup
    skip the parser entirely: there is nothing to decode or remove.
    """
    if '<' not in fragment and '&' not in fragment:
        return _lines(fragment)
    soup = BeautifulSoup(fragment, 'html.parser')
    for element in soup.find_all(_INVISIBLE_TAGS):
        element.decompose()
    for element in soup.find_all(_BLOCK_TAGS):
        element.append('\n')
    return _lines(soup.get_text(''))


def _serialize(node: Any, depth: int = 0) -> str:
    """One real child node back into markup, keeping its local tag and attributes."""
    tag = node.tag
    if not isinstance(tag, str):
        # Comments and processing instructions are not reader content.
        return escape(node.tail or '')
    if depth >= _MAX_MARKUP_DEPTH:
        return escape(''.join(node.itertext()) + (node.tail or ''))
    tag = tag.rsplit('}', 1)[-1]
    attributes = ''.join(f' {name.rsplit("}", 1)[-1]}={quoteattr(str(value))}' for name, value in node.attrib.items())
    tail = escape(node.tail or '')
    if tag in _VOID_TAGS:
        return f'<{tag}{attributes}/>{tail}'
    outer = escape(node.text or '') + ''.join(_serialize(child, depth + 1) for child in node)
    return f'<{tag}{attributes}>{outer}</{tag}>{tail}'


def _markup_type(element: Any) -> str:
    """What a summary/body field says it holds.

    RSS has no media type: ``description`` and ``content:encoded`` are markup by
    convention, so a missing type there still means markup. Atom's default is
    ``text``, which means an Atom field that declares nothing holds literal text —
    parsing it as HTML would silently eat anything in it that looks like a tag.
    """
    declared = (element.get('type') or '').split(';')[0].strip().lower()
    if declared:
        return declared
    return 'text' if str(element.tag).startswith(f'{{{_NS["atom"]}}}') else 'html'


def _inner_markup(element: Any) -> str:
    """An element's content as markup, never the element itself.

    RSS ``description``/``content:encoded`` and an Atom ``type="html"`` field hold
    escaped markup as text, which the XML parser has already decoded. An Atom
    ``type="xhtml"`` field instead holds real child nodes: flattening those with
    ``itertext`` throws the paragraph structure away, and re-serializing the whole
    ``<content>`` element would hand the feed's own wrapper to the reader as body.
    A ``text`` field (Atom's default) is literal, so it is escaped on the way out:
    ``a <b> c`` is three words around a written-out angle bracket, not a bold ``b``.
    """
    if element is None:
        return ''
    if _markup_type(element) == 'text':
        return escape(''.join(element.itertext()))
    if len(element) == 0:
        return element.text or ''
    return escape(element.text or '') + ''.join(_serialize(child) for child in element)


def _summary_text(element: Any) -> str:
    """Text of a summary-ish element: ``text`` is literal, everything else markup."""
    if element is None:
        return ''
    if _markup_type(element) == 'text':
        return _lines(''.join(element.itertext()))
    return _visible_text(_inner_markup(element))


def _find(parent: Any, *names: str) -> Any:
    """The first of ``names`` that exists, understanding both plain and namespaced tags.

    A bare name is tried as written (RSS) and then in the Atom namespace, because an
    Atom document puts ``title`` and ``link`` in that namespace — a plain lookup finds
    a body but not the title, which reads as a feed with unnamed entries.
    """
    if parent is None:
        return None
    for name in names:
        if ':' in name:
            namespace, _, local = name.partition(':')
            found = parent.find(f'{{{_NS[namespace]}}}{local}')
        else:
            found = parent.find(name)
            if found is None:
                found = parent.find(f'{{{_NS["atom"]}}}{name}')
        if found is not None:
            return found
    return None


def _attr(element: Any, name: str, attribute: str = 'href') -> str | None:
    if element is None:
        return None
    namespace, _, local = name.partition(':')
    found = element.find(f'{{{_NS[namespace]}}}{local}')
    if found is None:
        found = element.find(local)
    if found is None:
        return None
    value = found.get(attribute) or _text(found)
    return value or None


def _image_url(channel: Any, item: Any) -> str | None:
    itunes_image = _attr(item, 'itunes:image') or _attr(channel, 'itunes:image')
    if itunes_image:
        return itunes_image
    image = channel.find('image/url')
    if image is not None and _text(image):
        return _text(image)
    media = _find(item, 'media:thumbnail', 'media:content')
    if media is not None:
        return media.get('url') or None
    return None


def _transcript(item: Any) -> tuple[str | None, str | None]:
    """The first usable ``<podcast:transcript>``, if the episode publishes one."""
    for name in ('podcast:transcript', 'podcast-legacy:transcript'):
        namespace, _, local = name.partition(':')
        for element in item.findall(f'{{{_NS[namespace]}}}{local}'):
            url = element.get('url')
            kind = (element.get('type') or '').split(';')[0].strip().lower()
            if url and (not kind or kind in _TRANSCRIPT_TYPES):
                return url, kind or 'text/vtt'
    return None, None


def _enclosure(item: Any) -> tuple[str | None, str | None]:
    """RSS uses ``<enclosure>``; Atom uses ``<link rel="enclosure">``."""
    enclosure = item.find('enclosure')
    if enclosure is not None:
        return enclosure.get('url') or None, enclosure.get('type') or None
    for link in item.findall(f'{{{_NS["atom"]}}}link'):
        if link.get('rel') == 'enclosure':
            return link.get('href') or None, link.get('type') or None
    return None, None


def _episode(item: Any, channel: Any) -> Episode | None:
    # `_find` 而不是 `item.find`：Atom 的 title/link 都带命名空间，裸 find 找不到，
    # 于是 Atom 条目标题会变成「(无标题)」（正文却是读得到的）。
    title = _text(_find(item, 'title')) or '(无标题)'
    audio_url, audio_type = _enclosure(item)
    transcript_url, transcript_type = _transcript(item)
    link = _text(item.find('link')) or ''
    if not link:
        atom_link = item.find(f'{{{_NS["atom"]}}}link')
        link = (atom_link.get('href') if atom_link is not None else '') or audio_url or ''
    guid = _text(item.find('guid')) or _text(item.find(f'{{{_NS["atom"]}}}id')) or ''
    if not guid:
        guid = audio_url or hashlib.sha1(f'{title}{_text(item.find("pubDate"))}'.encode()).hexdigest()[:16]
    published = parse_published(_text(item.find('pubDate')) or _text(item.find(f'{{{_NS["atom"]}}}published')))
    duration = parse_duration(_text(_find(item, 'itunes:duration')))
    # RSS spells the summary ``description``, Atom spells it ``summary``; both are
    # "what this episode says about itself". The digest is text, so the tags a feed
    # ships in there must not become part of it. An empty one falls through to the
    # next spelling rather than shadowing it.
    summary = ''
    for name in ('description', 'summary', 'itunes:summary'):
        summary = _summary_text(_find(item, name))
        if summary:
            break
    # The body stays markup, because that is what the reader renders: RSS keeps it in
    # ``content:encoded``, Atom in ``<content>``.
    body_html = _inner_markup(_find(item, 'content:encoded', 'content')).strip() or None
    if not summary and body_html:
        # With no summary the body is the only thing to summarize from. The body
        # itself is kept in full either way.
        summary = _visible_text(body_html)
    return Episode(
        guid=guid,
        title=title,
        link=link,
        audio_url=audio_url or None,
        audio_type=audio_type or None,
        published_at=published,
        duration_seconds=duration,
        image_url=_image_url(channel, item),
        description=summary or None,
        transcript_url=transcript_url,
        transcript_type=transcript_type,
        body_html=body_html,
    )


def _decompress(payload: bytes, *, limit: int) -> bytes:
    """Un-gzip with a ceiling on what comes out.

    ``gzip.decompress`` would expand a bomb in one go, after which a size check is
    pointless. The stream is read a block at a time and stopped at the limit, so the
    memory a feed can ask for stays bounded.
    """
    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise PodcastError(f'解压后超过 {max(limit // 1024 // 1024, 1)} MB 上限')
    return data


def parse_feed(payload: bytes | str) -> Feed:
    """Read an RSS or Atom podcast feed.

    Feed shapes vary more than the spec suggests: namespaces are prefixed
    differently, some fields are attributes and some are elements, and dates come
    in several formats. All of that is handled here so the rest of hippo sees one
    shape.
    """
    if isinstance(payload, bytes) and payload[:2] == b'\x1f\x8b':
        payload = _decompress(payload, limit=MAX_FEED_BYTES)
    text = payload.decode('utf-8', 'replace') if isinstance(payload, bytes) else payload
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise PodcastError(f'订阅内容不是合法的 XML：{exc}') from exc

    channel = root.find('channel')
    if channel is None and root.tag == f'{{{_NS["atom"]}}}feed':
        # Atom 的根元素本身就是 feed，不是它的子元素
        channel = root
    if channel is None:
        raise PodcastError('订阅内容既不是 RSS 也不是 Atom')

    is_atom = channel.tag.startswith(f'{{{_NS["atom"]}}}')
    if is_atom:
        title = _text(channel.find(f'{{{_NS["atom"]}}}title'))
        items = channel.findall(f'{{{_NS["atom"]}}}entry')
        link = ''
        for candidate in channel.findall(f'{{{_NS["atom"]}}}link'):
            link = candidate.get('href') or ''
            if candidate.get('rel') in (None, 'alternate'):
                break
        image = _attr(channel, 'itunes:image')
    else:
        title = _text(channel.find('title'))
        items = channel.findall('item')
        link = _text(channel.find('link'))
        image = _image_url(channel, None)

    episodes = [episode for item in items if (episode := _episode(item, channel)) is not None]
    if not episodes:
        raise PodcastError('这个订阅里没有条目')
    return Feed(title=title or '未命名播客', link=link, image_url=image, episodes=episodes)


class PodcastClient:
    def __init__(self, *, proxy: str = '', client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=FEED_TIMEOUT, follow_redirects=True, proxy=proxy or None)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _fetch(self, url: str, *, limit: int, timeout: float) -> bytes:
        try:
            async with self._client.stream('GET', url, timeout=timeout) as response:
                if response.status_code != 200:
                    raise PodcastError(f'{url} 返回 HTTP {response.status_code}')
                payload = bytearray()
                async for piece in response.aiter_bytes(1 << 16):
                    payload.extend(piece)
                    if len(payload) > limit:
                        raise PodcastError(f'{url} 超过 {limit // 1024 // 1024} MB 上限')
        except httpx.HTTPError as exc:
            raise PodcastError(f'{url} 拉取失败：{exc}') from exc
        return bytes(payload)

    async def feed(self, url: str) -> Feed:
        return parse_feed(await self._fetch(url, limit=MAX_FEED_BYTES, timeout=FEED_TIMEOUT))

    async def transcript(self, url: str) -> str:
        payload = await self._fetch(url, limit=MAX_TRANSCRIPT_BYTES, timeout=TRANSCRIPT_TIMEOUT)
        if payload[:2] == b'\x1f\x8b':
            payload = _decompress(payload, limit=MAX_TRANSCRIPT_BYTES)
        return payload.decode('utf-8', 'replace')


class PodcastSource:
    source = SOURCE

    def __init__(self, client: PodcastClient) -> None:
        self._client = client

    async def preview(self, ref: str) -> ChannelInfo:
        url = normalize_feed_url(ref)
        feed = await self._client.feed(url)
        # 有音频附件的是播客，只有正文的是图文订阅：同一套解析，只差一个标签。
        # 判定放在这里，因为只有抓到 feed 才知道。
        kind = SOURCE if any(episode.audio_url for episode in feed.episodes) else FEED_SOURCE
        return ChannelInfo(
            biz=feed_biz(url, kind),
            nickname=feed.title,
            source_key=url,
            avatar_url=feed.image_url,
            source=kind,
        )

    async def list_items(self, *, source_key: str, limit: int, cursor: str | None = None) -> Listing:
        # 播客没有「翻页」：feed 本身就是全量（按新到旧），第一页就是最新的 N 条。
        feed = await self._client.feed(source_key)
        offset = int(cursor) if cursor and cursor.isdigit() else 0
        window = feed.episodes[offset : offset + max(int(limit), 1)]
        items = [
            Item(
                article_id=episode.guid,
                title=episode.title,
                link=episode.link or episode.audio_url or source_key,
                # 纯文本条目（博客 feed）没有音频：media_url 留空，于是不会排转写任务。
                media_url=episode.audio_url or ('' if episode.body_html else (episode.link or source_key)),
                publish_at=episode.published_at,
                cover=episode.image_url or feed.image_url,
                digest=episode.description,
                author=feed.title,
                duration_seconds=episode.duration_seconds,
                raw={
                    'guid': episode.guid,
                    'audio_url': episode.audio_url,
                    'transcript_url': episode.transcript_url,
                    'transcript_type': episode.transcript_type,
                    'duration_seconds': episode.duration_seconds,
                },
                content_html=episode.body_html,
            )
            for episode in window
        ]
        consumed = offset + len(items)
        exhausted = consumed >= len(feed.episodes)
        return Listing(items=items, next_cursor=None if exhausted else str(consumed), is_end=exhausted)


class PodcastResolver:
    """Use the feed's transcript when there is one, and the audio otherwise."""

    def __init__(self, client: PodcastClient, *, proxy: str = '') -> None:
        self._client = client
        self._proxy = proxy

    async def resolve(self, job) -> ResolvedMedia:
        payload = job.get('payload') or {}
        if isinstance(payload, str):
            payload = {}
        duration = payload.get('duration_seconds')
        transcript_url = '' if payload.get('skip_subtitle') else str(payload.get('transcript_url') or '')
        if transcript_url:
            subtitle = await self._transcript(transcript_url)
            if subtitle is not None:
                # 音频地址照样带上：现成转录稿只省掉 ASR，留档仍然要做（pipeline 决定用途）。
                return ResolvedMedia(
                    subtitle=subtitle,
                    audio_url=str(job.get('media_url') or '') or None,
                    proxy=self._proxy or None,
                    duration_seconds=duration,
                )

        audio_url = str(job.get('media_url') or '')
        if not audio_url:
            raise MediaJobSkipped('这一期没有音频地址')
        return ResolvedMedia(
            audio_url=audio_url,
            proxy=self._proxy or None,
            duration_seconds=duration,
        )

    async def _transcript(self, url: str) -> Subtitle | None:
        try:
            payload = await self._client.transcript(url)
        except PodcastError as exc:
            logger.warning('转录稿取不到，改用转写：%s', exc)
            return None
        cues = parse_timed_text(payload)
        if not cues:
            logger.info('转录稿没有解析出任何字幕行，改用转写：%s', url)
            return None
        logger.info('播客用现成转录稿（%d 段）', len(cues))
        return Subtitle(blocks=subtitle_blocks(cues), markdown=subtitle_markdown(cues))


__all__ = [
    'FEED_SOURCE',
    'FEED_TIMEOUT',
    'MAX_FEED_BYTES',
    'MAX_TRANSCRIPT_BYTES',
    'SOURCE',
    'Episode',
    'Feed',
    'PodcastClient',
    'PodcastError',
    'PodcastResolver',
    'PodcastSource',
    'feed_biz',
    'normalize_feed_url',
    'parse_duration',
    'parse_feed',
    'parse_published',
]
