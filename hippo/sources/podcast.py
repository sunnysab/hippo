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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from defusedxml import ElementTree

from ..logger import get_logger
from ..media.pipeline import MediaJobSkipped, ResolvedMedia, Subtitle
from ..media.subtitle import blocks as subtitle_blocks
from ..media.subtitle import markdown as subtitle_markdown
from ..media.subtitle import parse_timed_text
from .ingest import ChannelInfo, Item, Listing

logger = get_logger(__name__)

SOURCE = 'podcast'

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


class PodcastError(RuntimeError):
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


def feed_biz(feed_url: str) -> str:
    """A stable ``biz`` for a feed URL, since the URL itself is long and mutable."""
    digest = hashlib.sha1(feed_url.strip().encode('utf-8')).hexdigest()
    return f'{SOURCE}:{digest[:16]}'


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


def _find(parent: Any, *names: str) -> Any:
    if parent is None:
        return None
    for name in names:
        namespace, _, local = name.partition(':')
        found = parent.find(f'{{{_NS[namespace]}}}{local}')
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
    title = _text(item.find('title')) or '(无标题)'
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
    return Episode(
        guid=guid,
        title=title,
        link=link,
        audio_url=audio_url or None,
        audio_type=audio_type or None,
        published_at=published,
        duration_seconds=duration,
        image_url=_image_url(channel, item),
        description=_text(item.find('description')) or None,
        transcript_url=transcript_url,
        transcript_type=transcript_type,
        body_html=_text(_find(item, 'content:encoded')) or None,
    )


def parse_feed(payload: bytes | str) -> Feed:
    """Read an RSS or Atom podcast feed.

    Feed shapes vary more than the spec suggests: namespaces are prefixed
    differently, some fields are attributes and some are elements, and dates come
    in several formats. All of that is handled here so the rest of hippo sees one
    shape.
    """
    if isinstance(payload, bytes) and payload[:2] == b'\x1f\x8b':
        payload = gzip.decompress(payload)
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
            payload = gzip.decompress(payload)
        return payload.decode('utf-8', 'replace')


class PodcastSource:
    source = SOURCE

    def __init__(self, client: PodcastClient) -> None:
        self._client = client

    async def preview(self, ref: str) -> ChannelInfo:
        url = normalize_feed_url(ref)
        feed = await self._client.feed(url)
        return ChannelInfo(
            biz=feed_biz(url),
            nickname=feed.title,
            source_key=url,
            avatar_url=feed.image_url,
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
                media_url=episode.audio_url or episode.link or source_key,
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
