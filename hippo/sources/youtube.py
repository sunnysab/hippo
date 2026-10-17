"""YouTube, through ``yt-dlp``.

``yt-dlp`` rather than the API: it already knows how to extract what we need, it is
maintained against YouTube's frequent changes, and the alternative (an API key plus
our own extractor) is a much bigger thing to keep alive. It is invoked as a
subprocess so the package stays out of ``pyproject`` and the binary can be updated
by the system package manager.

Two consequences of running on someone else's extractor, both handled here:

* **Failures are per-invocation and often global.** "Sign in to confirm you're not
  a bot" looks exactly like a deleted video from here. The queue's retry ceiling
  bounds the damage, and the reason is kept verbatim so the operator can tell the
  two apart by reading it.
* **The CDN needs the same proxy as the site.** The audio URL ``yt-dlp`` hands back
  points at ``googlevideo.com``, which is just as unreachable as the site itself, so
  the proxy travels with the resolved media rather than only with the listing.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..logger import get_logger
from ..media.pipeline import MediaJobSkipped, ResolvedMedia
from . import SourceError
from .ingest import ChannelInfo, Item, Listing

logger = get_logger(__name__)

SOURCE = 'youtube'
#: yt-dlp is a dependency, so the binary sits next to the interpreter, but a
#: system-wide one (fresher, or the operator's own) wins when PATH has it.
YTDLP_NAME = 'yt-dlp'
WATCH_BASE = 'https://www.youtube.com/watch?v='

#: A logged-in session is what keeps Google from answering with the bot check, so a
#: submitted cookie is worth carrying into every call. It is written to a private
#: temporary file because ``--cookies`` only accepts a path.
COOKIE_FILE_PREFIX = 'hippo-ytdlp-cookies-'

#: One channel page per listing call. yt-dlp's channel tabs are continuable, so a
#: window of items is enough for the periodic sync; deeper history is a backfill.
LIST_TIMEOUT = 300.0
#: Metadata for one video. Extraction is the slow part, not the network, but a hung
#: invocation must not hold the queue forever.
PROBE_TIMEOUT = 180.0

#: Ceiling on the picture Hippo keeps: 720p is the best value the download budget and
#: the reader's bandwidth want, so a taller track is not chosen at all.
VIDEO_MAX_HEIGHT = 720
#: The container the pipeline can mux and serve without re-encoding the video.
VIDEO_EXTENSIONS = frozenset({'mp4', 'm4v'})
#: yt-dlp's codec prefix for AAC, the audio an MP4 accepts without re-encoding.
AAC_CODEC_PREFIX = 'mp4a'
#: Protocols of a single downloadable file. HLS/DASH carry their own protocol, but a URL
#: can still point at a manifest under ``https``, so the path is checked as well.
_DIRECT_PROTOCOLS = frozenset({'http', 'https'})

_CHANNEL_ID_RE = re.compile(r'^UC[0-9A-Za-z_-]{22}$')
_VIDEO_ID_RE = re.compile(r'^[0-9A-Za-z_-]{11}$')


class YoutubeError(SourceError):
    pass


def video_id_from_url(value: str) -> str | None:
    """The 11-character video id in a watch/youtu.be/shorts URL."""
    parsed = urlparse((value or '').strip())
    host = (parsed.hostname or '').lower()
    if host in {'youtu.be', 'www.youtu.be'}:
        candidate = parsed.path.lstrip('/').split('/')[0]
        return candidate if _VIDEO_ID_RE.match(candidate) else None
    if host not in {'www.youtube.com', 'youtube.com', 'm.youtube.com', 'music.youtube.com'}:
        return None
    if parsed.path in {'/watch', '/watch/'}:
        candidate = (parse_qs(parsed.query).get('v') or [''])[0]
        return candidate if _VIDEO_ID_RE.match(candidate) else None
    match = re.match(r'^/(?:shorts|live|embed)/([0-9A-Za-z_-]{11})', parsed.path)
    return match.group(1) if match else None


def channel_id_from_url(value: str) -> str | None:
    """A ``/channel/UC…`` id straight out of the URL, when it is there."""
    parsed = urlparse((value or '').strip())
    match = re.match(r'^/channel/(UC[0-9A-Za-z_-]{22})', parsed.path)
    return match.group(1) if match else None


def embed_url(video_id: str) -> str:
    return f'https://www.youtube.com/embed/{video_id}'


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


def _has_audio(fmt: dict[str, Any]) -> bool:
    return str(fmt.get('acodec') or '').lower() not in {'', 'none'}


def _is_h264(fmt: dict[str, Any]) -> bool:
    return str(fmt.get('vcodec') or '').split('.')[0].lower() in {'avc1', 'avc', 'h264'}


def _is_audio_only(fmt: dict[str, Any]) -> bool:
    return _has_audio(fmt) and fmt.get('vcodec') in (None, 'none')


def _matches_codec(fmt: dict[str, Any], codec: str | None) -> bool:
    return codec is None or str(fmt.get('acodec') or '').startswith(codec)


def best_audio_format(
    formats: Sequence[dict[str, Any]], *, codec: str | None = None, urls_only: bool = False
) -> dict[str, Any] | None:
    """The highest-bitrate audio-only format.

    ``codec`` narrows it to one family (``mp4a`` for the AAC that can be muxed into an
    MP4 without re-encoding); by default any audio-only track is eligible. ``urls_only``
    additionally requires a directly downloadable, non-manifest URL — the split mux
    needs both, and a bad entry must not hide a usable lower-bitrate one.
    """
    audio_only = [
        fmt
        for fmt in formats
        if _is_audio_only(fmt) and _matches_codec(fmt, codec) and (not urls_only or _is_direct_file(fmt))
    ]
    if not audio_only:
        return None
    return max(audio_only, key=lambda fmt: float(fmt.get('abr') or 0))


def _direct_url(url: str) -> bool:
    """An HTTP(S) URL with a host whose path is not an HLS/DASH manifest."""
    if url.lower().split('?', 1)[0].endswith(('.m3u8', '.mpd')):
        return False
    parsed = urlparse(url)
    return parsed.scheme in _DIRECT_PROTOCOLS and bool(parsed.hostname)


def _is_direct_file(fmt: dict[str, Any]) -> bool:
    """A single HTTP(S) file, never a manifest or a fragment-only entry.

    A DRM stream has no URL a plain GET can use. HLS/DASH carry their own protocol, but
    an ``https`` protocol does not prove the URL is a file: the path still has to name a
    host and must not end in ``.m3u8``/``.mpd``.
    """
    if fmt.get('has_drm') or fmt.get('manifest_url') or fmt.get('fragments'):
        return False
    if str(fmt.get('protocol') or '').lower() not in _DIRECT_PROTOCOLS:
        return False
    return _direct_url(str(fmt.get('url') or ''))


def _video_height(fmt: dict[str, Any]) -> int:
    try:
        return int(fmt.get('height') or 0)
    except TypeError, ValueError:
        return 0


def _mp4_video(fmt: dict[str, Any]) -> bool:
    """A directly downloadable H.264 MP4 at most ``VIDEO_MAX_HEIGHT`` tall."""
    return (
        str(fmt.get('ext') or '').lower() in VIDEO_EXTENSIONS
        and _is_h264(fmt)
        and _is_direct_file(fmt)
        and 0 < _video_height(fmt) <= VIDEO_MAX_HEIGHT
    )


def _progressive_aac(fmt: dict[str, Any]) -> bool:
    """The picture carries its own AAC, so it plays as a native MP4 with no muxing."""
    return _has_audio(fmt) and _matches_codec(fmt, AAC_CODEC_PREFIX)


def best_video_format(formats: Sequence[dict[str, Any]], *, require_audio: bool = False) -> dict[str, Any] | None:
    """The best H.264 MP4 the pipeline can download, mux and serve.

    The picture must be H.264 in an MP4/M4V, a direct HTTP(S) file (not DRM, not an
    HLS/DASH manifest), and at most ``VIDEO_MAX_HEIGHT`` tall: a taller track is
    skipped, never rounded down. The tallest wins; at equal height a file that already
    carries AAC wins, then any file with audio, because it needs the least work.
    ``require_audio`` keeps only progressive tracks whose own audio is AAC.
    """
    eligible = [fmt for fmt in formats if _mp4_video(fmt) and (not require_audio or _progressive_aac(fmt))]
    if not eligible:
        return None
    return max(eligible, key=lambda fmt: (_video_height(fmt), _progressive_aac(fmt), _has_audio(fmt)))


def ytdlp_bin() -> str:
    """Locate ``yt-dlp``: PATH first, then this interpreter's own environment.

    The service is started by systemd, which never has the venv's ``bin/`` on PATH,
    so the fallback is what makes a from-pyproject install usable.
    """
    found = shutil.which(YTDLP_NAME)
    if found:
        return found
    sibling = Path(sys.executable).with_name(YTDLP_NAME)
    return str(sibling) if sibling.is_file() else YTDLP_NAME


def write_cookie_file(cookies: str) -> Path | None:
    """Put a submitted cookie where ``yt-dlp`` can read it, and nowhere else.

    0600 in the system temp dir: it is a live login for a Google account.
    """
    if not cookies.strip():
        return None
    handle, name = tempfile.mkstemp(prefix=COOKIE_FILE_PREFIX, suffix='.txt')
    path = Path(name)
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, 'w', encoding='utf-8') as file:
            file.write(cookies if cookies.endswith('\n') else f'{cookies}\n')
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


async def run_ytdlp(
    args: Sequence[str],
    *,
    proxy: str = '',
    timeout: float = PROBE_TIMEOUT,
    cookies: str = '',
) -> str:
    """Run ``yt-dlp`` and return its stdout.

    stderr is kept in the error because that is where yt-dlp explains *why*
    (bot check, deleted video, geo-block), and an operator needs to read it.
    """
    cookie_path = write_cookie_file(cookies)
    try:
        return await _run_ytdlp(args, proxy=proxy, timeout=timeout, cookie_path=cookie_path)
    finally:
        if cookie_path is not None:
            cookie_path.unlink(missing_ok=True)


async def _run_ytdlp(
    args: Sequence[str],
    *,
    proxy: str,
    timeout: float,
    cookie_path: Path | None,
) -> str:
    command = [ytdlp_bin(), '--no-warnings', '--no-progress']
    if proxy:
        command += ['--proxy', proxy]
    if cookie_path is not None:
        command += ['--cookies', str(cookie_path)]
    command += list(args)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise YoutubeError('未找到 yt-dlp，请先安装（uv sync 会装进虚拟环境）') from exc
    except OSError as exc:  # pragma: no cover - defensive: exec failures only
        raise YoutubeError(f'无法启动 yt-dlp：{exc}') from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise YoutubeError(f'yt-dlp 超过 {timeout:.0f} 秒没有返回') from exc
    if process.returncode != 0:
        detail = (stderr or b'').decode('utf-8', 'replace').strip()[-600:]
        raise YoutubeError(f'yt-dlp 失败（退出码 {process.returncode}）：{detail}')
    return (stdout or b'').decode('utf-8', 'replace')


def parse_json_lines(output: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            entries.append(payload)
    return entries


class YoutubeSource:
    """List a channel's videos, and describe a channel from a pasted URL."""

    source = SOURCE

    def __init__(self, *, proxy: str = '', cookies: str = '') -> None:
        self._proxy = proxy
        self._cookies = cookies

    async def preview(self, ref: str) -> ChannelInfo:
        channel_id = channel_id_from_url(ref)
        if channel_id:
            return ChannelInfo(biz=f'youtube:{channel_id}', nickname=channel_id, source_key=channel_id)
        url = ref if ref.startswith('http') else f'https://www.youtube.com/{ref.lstrip("@")}'
        output = await run_ytdlp(
            ['--flat-playlist', '--dump-json', '-I', '1:1', url],
            proxy=self._proxy,
            timeout=LIST_TIMEOUT,
            cookies=self._cookies,
        )
        entries = parse_json_lines(output)
        if not entries:
            raise YoutubeError(f'从 {ref} 没能解析出频道')
        entry = entries[0]
        channel_id = str(entry.get('playlist_channel_id') or '')
        if not _CHANNEL_ID_RE.match(channel_id):
            raise YoutubeError(f'{ref} 不是频道链接（看起来是一个视频或播放列表）')
        nickname = str(entry.get('playlist_channel') or entry.get('playlist_uploader') or channel_id)
        return ChannelInfo(biz=f'youtube:{channel_id}', nickname=nickname, source_key=channel_id)

    async def list_items(self, *, source_key: str, limit: int, cursor: str | None = None) -> Listing:
        if not _CHANNEL_ID_RE.match(source_key or ''):
            raise YoutubeError(f'{source_key!r} 不是 YouTube 频道 id')
        start = int(cursor) if cursor and cursor.isdigit() else 1
        end = start + max(int(limit), 1) - 1
        output = await run_ytdlp(
            [
                '--flat-playlist',
                '--dump-json',
                # 列表接口不返回发布日期；这个 extractor 参数让 yt-dlp 给出近似日期，
                # 否则视频会以「没有发布时间」入库、排序时掉到最后。
                '--extractor-args',
                'youtubetab:approximate_date',
                '-I',
                f'{start}:{end}',
                f'https://www.youtube.com/channel/{source_key}/videos',
            ],
            proxy=self._proxy,
            timeout=LIST_TIMEOUT,
            cookies=self._cookies,
        )
        entries = parse_json_lines(output)
        items: list[Item] = []
        for entry in entries:
            video_id = str(entry.get('id') or '')
            if not _VIDEO_ID_RE.match(video_id):
                continue
            thumbnails = entry.get('thumbnails') or []
            items.append(
                Item(
                    article_id=video_id,
                    title=str(entry.get('title') or video_id),
                    link=f'{WATCH_BASE}{video_id}',
                    media_url=f'{WATCH_BASE}{video_id}',
                    publish_at=int(entry.get('timestamp') or 0) or None,
                    cover=str((thumbnails[-1] or {}).get('url') or '') if thumbnails else None,
                    digest=None,
                    author=None,
                    duration_seconds=int(entry.get('duration') or 0) or None,
                    raw={'id': video_id},
                )
            )
        # 用完一页就到底：yt-dlp 不回总条数，少给一条就当结束，避免无限翻页。
        exhausted = len(items) < max(int(limit), 1)
        return Listing(items=items, next_cursor=None if exhausted else str(end + 1), is_end=exhausted)


class YoutubeResolver:
    """Turn a queued watch URL into the audio the pipeline downloads."""

    def __init__(self, *, proxy: str = '', cookies: str = '') -> None:
        self._proxy = proxy
        self._cookies = cookies

    async def resolve(self, job) -> ResolvedMedia:
        video_id = video_id_from_url(str(job.get('media_url') or ''))
        if not video_id:
            raise MediaJobSkipped(f'无法从 {job.get("media_url")} 里取出视频 id')
        output = await run_ytdlp(
            ['--dump-json', '--no-playlist', f'{WATCH_BASE}{video_id}'],
            proxy=self._proxy,
            cookies=self._cookies,
        )
        entries = parse_json_lines(output)
        if not entries:
            raise MediaJobSkipped(f'视频 {video_id} 取不到信息（可能已删除或受限）')
        entry = entries[0]
        formats = entry.get('formats') or []
        video = best_video_format(formats)
        audio = best_audio_format(formats)
        fallback_audio = ''
        if video is not None and not _progressive_aac(video):
            # The picture is silent, or carries audio that is not AAC: it needs an
            # external AAC to mux. One unfetchable high-bitrate entry must not hide a
            # usable lower-bitrate one, so the whole set is filtered before the max.
            mux_audio = best_audio_format(formats, codec=AAC_CODEC_PREFIX, urls_only=True)
            if mux_audio is None:
                # Nothing to mux: drop the picture, but keep whatever audio exists —
                # a progressive file still transcribes even when it cannot be served.
                fallback_audio = str(video.get('url') or '') if _has_audio(video) else ''
                video = None
            else:
                audio = mux_audio
        has_audio = video is not None and _progressive_aac(video)
        video_url = str(video.get('url') or '') if video is not None else ''
        # A progressive AAC file is its own audio source (also when keep_media is
        # false); a split picture pairs with the AAC picked above.
        split_url = str(audio.get('url') or '') if audio is not None else ''
        audio_url = video_url if has_audio else (split_url or fallback_audio)
        if not audio_url:
            raise MediaJobSkipped(f'视频 {video_id} 没有可下载的音轨')
        duration = int(entry.get('duration') or 0) or None
        return ResolvedMedia(
            audio_url=audio_url,
            video_url=video_url or None,
            video_has_audio=has_audio,
            headers={'User-Agent': 'Mozilla/5.0'},
            proxy=self._proxy or None,
            duration_seconds=duration,
            embed_block=video_block(video_id, cover_url=str(entry.get('thumbnail') or '') or None),
        )


__all__ = [
    'PROBE_TIMEOUT',
    'SOURCE',
    'YTDLP_NAME',
    'YoutubeError',
    'YoutubeResolver',
    'YoutubeSource',
    'best_audio_format',
    'best_video_format',
    'channel_id_from_url',
    'embed_url',
    'parse_json_lines',
    'run_ytdlp',
    'video_block',
    'video_id_from_url',
    'write_cookie_file',
    'ytdlp_bin',
]
