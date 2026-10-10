"""Bilibili: a UP's videos, and the audio underneath one of them.

Ported from ``bili-transcribe``'s standalone client, plus the listing half it did
not have. Two things about this source are worth knowing before reading the code:

* **The投稿列表接口 is risk-controlled.** ``/x/space/wbi/arc/search`` answers 412 or
  -352 without a real browser session, which is why the cookie is part of the
  settings and why a failure says so instead of looking like a network problem.
  The per-video endpoints (``view``, ``playurl``) answer fine without one.
* **Playback URLs expire.** ``playurl`` hands out signed CDN links, so the audio
  is resolved at transcription time from the page URL, never stored in the queue.
"""

from __future__ import annotations

import re
import time
from hashlib import md5
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx

from ..logger import get_logger
from ..media.pipeline import MediaJobSkipped, ResolvedMedia, Subtitle
from .ingest import ChannelInfo, Item, Listing

logger = get_logger(__name__)

SOURCE = 'bilibili'
API_BASE = 'https://api.bilibili.com'
WEB_BASE = 'https://www.bilibili.com'
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36'
REFERER = f'{WEB_BASE}/'

_BV_RE = re.compile(r'^BV[0-9A-Za-z]{10}$')
_AV_RE = re.compile(r'^av(\d+)$')
_VIDEO_PATH_RE = re.compile(r'^/video/(BV[0-9A-Za-z]{10}|av\d+)')
_SPACE_PATH_RE = re.compile(r'^/(\d+)')

#: The permutation Bilibili's WBI signature is built from. 其长度与内容由官方文档固定
#: （docs/misc/sign/wbi.md），改不了，也不需要改。
_MIXIN_TABLE = [
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
]

#: Ordinary DASH audio ids, cheapest first: 30216/30232/30280 are the public
#: tracks, the 30250/30251 pair is Dolby/FLAC and needs a paid account.
_ORDINARY_AUDIO_IDS = {30216, 30232, 30280}

#: Bilibili blocks a bare API client with this page; the message a user sees should
#: point at the cookie rather than at the network.
_RISK_CONTROL_HINT = 'B站风控拦截：投稿列表需要有效的 SESSDATA（在管理台「媒体与转写」里配置）'


class BilibiliError(RuntimeError):
    pass


def mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return ''.join(raw[index] for index in _MIXIN_TABLE if index < len(raw))[:32]


def sign_wbi(params: dict[str, Any], img_key: str, sub_key: str, *, wts: int | None = None) -> dict[str, str]:
    """Sign params the way Bilibili's WBI endpoints expect."""
    signed = {key: str(value) for key, value in params.items()}
    signed['wts'] = str(int(wts if wts is not None else time.time()))
    signed = {
        key: value.translate(str.maketrans({'!': '', "'": '', '(': '', ')': '', '*': ''}))
        for key, value in signed.items()
    }
    query = urlencode(sorted(signed.items()), quote_via=quote, safe='')
    signed['w_rid'] = md5((query + mixin_key(img_key, sub_key)).encode()).hexdigest()
    return signed


def parse_ref(value: str) -> tuple[str, str]:
    """Accept a video URL or an UP's space URL.

    Returns ``('video', bvid_or_aid)`` or ``('space', mid)``. Only public,
    single-page video URLs are understood, matching what the reader would paste.
    """
    parsed = urlparse(value.strip())
    if parsed.scheme not in {'http', 'https'}:
        raise ValueError('请输入 B站 的完整链接')
    host = (parsed.hostname or '').lower()
    if host not in {'www.bilibili.com', 'bilibili.com', 'm.bilibili.com', 'space.bilibili.com'}:
        raise ValueError('这不像是一个 B站 链接')
    if host == 'space.bilibili.com':
        match = _SPACE_PATH_RE.match(parsed.path)
        if not match:
            raise ValueError('B站空间链接里没有 UP 的 mid')
        return 'space', match.group(1)
    match = _VIDEO_PATH_RE.match(parsed.path)
    if not match:
        raise ValueError('只支持 /video/BV… 或 space.bilibili.com/<mid> 链接')
    identity = match.group(1)
    page = parse_qs(parsed.query).get('p')
    if page and page != ['1']:
        raise ValueError('只支持单 P 视频；合集请在频道页订阅')
    return 'video', identity


def duration_from_length(value: Any) -> int | None:
    """``"12:34"`` / ``"1:02:03"`` → seconds.

    The listing gives the length as text and no numeric duration, which is the one
    field the article list wants for its badge.
    """
    text = str(value or '').strip()
    if not text:
        return None
    parts = text.split(':')
    if not all(part.isdigit() for part in parts) or len(parts) > 3:
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def best_audio_track(playurl: dict[str, Any]) -> dict[str, Any] | None:
    """The highest-bandwidth ordinary DASH audio track."""
    data = playurl.get('data') or {}
    tracks = ((data.get('dash') or {}).get('audio')) or []
    ordinary = [track for track in tracks if int(track.get('id') or 0) in _ORDINARY_AUDIO_IDS]
    candidates = ordinary or tracks
    if not candidates:
        return None
    return max(candidates, key=lambda track: int(track.get('bandwidth') or 0))


def embed_url(video_id: str) -> str:
    return f'https://player.bilibili.com/player.html?bvid={video_id}&autoplay=0&high_quality=1'


def video_block(video_id: str, *, cover_url: str | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {
        'type': 'video',
        'provider': SOURCE,
        'video_id': video_id,
        'embed_url': embed_url(video_id),
    }
    if cover_url:
        block['cover_url'] = cover_url
    return block


def subtitle_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn Bilibili's subtitle JSON into timestamped paragraph blocks.

    Unlike the ASR path these timestamps are real, so a paragraph starts where the
    speaker actually did.
    """
    blocks: list[dict[str, Any]] = []
    for entry in payload.get('body') or []:
        text = str(entry.get('content') or '').strip()
        if not text:
            continue
        try:
            start = float(entry.get('from') or 0.0)
        except TypeError, ValueError:
            continue
        blocks.append({'type': 'paragraph', 'text': text, 't': round(start, 1)})
    return blocks


class BilibiliClient:
    """The HTTP half. Everything about *what* to fetch lives in the adapter."""

    def __init__(
        self,
        *,
        cookie: str = '',
        proxy: str = '',
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        headers = {'User-Agent': USER_AGENT, 'Referer': REFERER}
        if cookie:
            headers['Cookie'] = cookie
        self._headers = headers
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            proxy=proxy or None,
        )
        self._owns_client = client is None
        self._wbi: tuple[str, str] | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        allow_codes: set[int] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.get(f'{API_BASE}{path}', params=params, headers=self._headers)
        except httpx.HTTPError as exc:
            raise BilibiliError(f'请求 B站 失败：{exc}') from exc
        if response.status_code in (412, 403):
            raise BilibiliError(_RISK_CONTROL_HINT)
        if response.status_code != 200:
            raise BilibiliError(f'B站在 {path} 上返回 HTTP {response.status_code}')
        try:
            payload = response.json()
        except ValueError as exc:
            raise BilibiliError(f'B站在 {path} 上返回了非 JSON 内容') from exc
        code = payload.get('code')
        if code in (-352, -412):
            raise BilibiliError(_RISK_CONTROL_HINT)
        if code not in (0, None) and code not in (allow_codes or set()):
            message = payload.get('message') or payload.get('msg') or ''
            raise BilibiliError(f'B站接口返回 {code}：{message}')
        return payload

    async def wbi_keys(self) -> tuple[str, str]:
        if self._wbi:
            return self._wbi
        # 未登录（-101）是正常情况：WBI 密钥仍然会下发，只是拿不到账号信息。
        payload = await self._get_json('/x/web-interface/nav', allow_codes={-101})
        images = ((payload.get('data') or {}).get('wbi_img')) or {}
        img_url = str(images.get('img_url') or '')
        sub_url = str(images.get('sub_url') or '')
        if not img_url or not sub_url:
            raise BilibiliError('B站没有返回 WBI 密钥')
        self._wbi = (Path(urlparse(img_url).path).stem, Path(urlparse(sub_url).path).stem)
        return self._wbi

    async def _get_signed(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        img_key, sub_key = await self.wbi_keys()
        return await self._get_json(path, sign_wbi(params, img_key, sub_key))

    async def video(self, identity: str) -> dict[str, Any]:
        if _BV_RE.match(identity):
            params: dict[str, Any] = {'bvid': identity}
        else:
            matched = _AV_RE.match(identity)
            if not matched:
                raise BilibiliError(f'{identity!r} 不是有效的 BV 或 av 号')
            params = {'aid': matched.group(1)}
        payload = await self._get_json('/x/web-interface/view', params)
        return payload.get('data') or {}

    async def page_list(self, bvid: str) -> list[dict[str, Any]]:
        payload = await self._get_json('/x/player/pagelist', {'bvid': bvid})
        return list(payload.get('data') or [])

    async def player(self, bvid: str, cid: int) -> dict[str, Any]:
        payload = await self._get_signed('/x/player/wbi/v2', {'bvid': bvid, 'cid': cid})
        return payload.get('data') or {}

    async def playurl(self, bvid: str, cid: int) -> dict[str, Any]:
        payload = await self._get_signed(
            '/x/player/wbi/playurl',
            {'bvid': bvid, 'cid': cid, 'fnval': 4048, 'fnver': 0, 'fourk': 1},
        )
        return payload.get('data') or {}

    async def fetch_json(self, url: str) -> dict[str, Any]:
        """Fetch a subtitle file by URL (``subtitle_url`` may arrive protocol-relative)."""
        target = url if url.startswith('http') else f'https:{url}'
        try:
            response = await self._client.get(target, headers=self._headers)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BilibiliError(f'字幕文件下载失败：{exc}') from exc

    async def space_videos(self, mid: str, *, page: int, size: int) -> dict[str, Any]:
        payload = await self._get_signed(
            '/x/space/wbi/arc/search',
            {'mid': mid, 'pn': page, 'ps': size, 'order': 'pubdate'},
        )
        return payload.get('data') or {}

    async def space_info(self, mid: str) -> dict[str, Any]:
        payload = await self._get_json('/x/space/acc/info', {'mid': mid})
        return payload.get('data') or {}


class BilibiliSource:
    """List a UP's videos, and describe a channel from a pasted URL."""

    source = SOURCE

    def __init__(self, client: BilibiliClient) -> None:
        self._client = client

    async def preview(self, ref: str) -> ChannelInfo:
        kind, identity = parse_ref(ref)
        if kind == 'space':
            # acc/info 也在风控范围内，拿不到就用 mid 当名字，不要因此拒绝订阅。
            try:
                info = await self._client.space_info(identity)
            except BilibiliError as exc:
                logger.info('取 UP 资料失败，用 mid 作昵称（%s）', exc)
                info = {}
            return ChannelInfo(
                biz=f'bilibili:{identity}',
                nickname=str(info.get('name') or identity),
                source_key=identity,
                avatar_url=str(info.get('face') or '') or None,
            )
        video = await self._client.video(identity)
        if not video:
            raise BilibiliError(f'视频 {identity} 取不到信息')
        owner = video.get('owner') or {}
        bvid = str(video.get('bvid') or identity)
        return ChannelInfo(
            biz=f'bilibili:{bvid}',
            nickname=str(owner.get('name') or bvid),
            source_key=bvid,
            avatar_url=str(owner.get('face') or '') or None,
        )

    async def list_items(self, *, source_key: str, limit: int, cursor: str | None = None) -> Listing:
        page = int(cursor) if cursor and cursor.isdigit() else 1
        data = await self._client.space_videos(source_key, page=page, size=max(limit, 1))
        rows = ((data.get('list') or {}).get('vlist')) or []
        page_info = data.get('page') or {}
        total = int(page_info.get('count') or 0)
        items: list[Item] = []
        for row in rows[:limit]:
            bvid = str(row.get('bvid') or '')
            if not bvid:
                continue
            items.append(
                Item(
                    article_id=bvid,
                    title=str(row.get('title') or bvid),
                    link=f'{WEB_BASE}/video/{bvid}',
                    media_url=f'{WEB_BASE}/video/{bvid}',
                    publish_at=int(row.get('created') or 0) or None,
                    cover=str(row.get('pic') or '') or None,
                    digest=str(row.get('description') or '') or None,
                    author=str(row.get('author') or '') or None,
                    duration_seconds=duration_from_length(row.get('length')),
                    raw={'bvid': bvid, 'aid': row.get('aid')},
                )
            )
        consumed = page * max(limit, 1)
        has_more = bool(total) and consumed < total
        return Listing(items=items, next_cursor=str(page + 1) if has_more else None, is_end=not has_more)


class BilibiliResolver:
    """Turn a queued video page into something the pipeline can transcribe."""

    def __init__(self, client: BilibiliClient, *, use_cc_subtitle: bool = True) -> None:
        self._client = client
        self._use_cc_subtitle = use_cc_subtitle

    async def resolve(self, job) -> ResolvedMedia:
        identity = ''
        parsed = urlparse(str(job.get('media_url') or ''))
        match = _VIDEO_PATH_RE.match(parsed.path)
        if match:
            identity = match.group(1)
        if not identity:
            raise MediaJobSkipped(f'无法从 {job.get("media_url")} 里取出视频号')

        video = await self._client.video(identity)
        if not video:
            raise MediaJobSkipped(f'视频 {identity} 已不可见')
        bvid = str(video.get('bvid') or identity)
        cover = str(video.get('pic') or '') or None
        embed = video_block(bvid, cover_url=cover)

        pages = video.get('pages') or []
        if len(pages) > 1:
            logger.info('视频 %s 有 %d 个分 P，只转第 1 个', bvid, len(pages))
        cid = int((pages[0].get('cid') if pages else video.get('cid')) or 0)
        if not cid:
            raise MediaJobSkipped(f'视频 {bvid} 没有可用的音轨')
        duration = int(video.get('duration') or 0) or None

        if self._use_cc_subtitle:
            subtitle = await self._cc_subtitle(bvid, cid)
            if subtitle is not None:
                return ResolvedMedia(embed_block=embed, subtitle=subtitle, duration_seconds=duration)

        data = await self._client.playurl(bvid, cid)
        track = best_audio_track({'data': data})
        if track is None:
            raise MediaJobSkipped(f'视频 {bvid} 没有返回音频轨')
        url = str(track.get('baseUrl') or track.get('base_url') or '')
        if not url:
            raise MediaJobSkipped(f'视频 {bvid} 的音频地址为空')
        return ResolvedMedia(
            audio_url=url,
            # 没有 Referer 的话 CDN 会 403。
            headers={'Referer': REFERER, 'User-Agent': USER_AGENT},
            duration_seconds=duration,
            embed_block=embed,
        )

    async def _cc_subtitle(self, bvid: str, cid: int) -> Subtitle | None:
        """The cheap path: a subtitle the uploader already provided."""
        try:
            player = await self._client.player(bvid, cid)
        except BilibiliError as exc:
            logger.info('取 CC 字幕失败，改用转写（%s）', exc)
            return None
        subtitles = ((player.get('subtitle') or {}).get('subtitles')) or []
        if not subtitles:
            return None
        url = str(subtitles[0].get('subtitle_url') or '')
        if not url:
            return None
        try:
            payload = await self._client.fetch_json(url)
        except BilibiliError as exc:
            logger.warning('字幕文件取不到，改用转写：%s', exc)
            return None
        blocks = subtitle_blocks(payload)
        if not blocks:
            return None
        markdown = '\n\n'.join(block['text'] for block in blocks)
        logger.info('视频 %s 用 CC 字幕（%d 段）', bvid, len(blocks))
        return Subtitle(blocks=blocks, markdown=markdown)


__all__ = [
    'SOURCE',
    'BilibiliClient',
    'BilibiliError',
    'BilibiliResolver',
    'BilibiliSource',
    'best_audio_track',
    'duration_from_length',
    'embed_url',
    'mixin_key',
    'parse_ref',
    'sign_wbi',
    'subtitle_blocks',
    'video_block',
]
