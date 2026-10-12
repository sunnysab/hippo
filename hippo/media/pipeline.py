"""Turning one queued media job into an article body.

The flow is the one PLAN §5.1 describes: take the cheap path when it exists (a
podcast transcript, a CC subtitle), otherwise download the audio, archive it, cut
it into chunks, transcribe them one at a time, and stitch the pieces back together
with the timestamps a reader navigates by.

Failures are sorted by whose fault they are, because the caller reacts differently:

* ``MediaJobSkipped`` — nothing to do (no audio, longer than the ceiling). The job
  is done as far as the queue is concerned.
* ``MediaJobFailure`` — this article's fault (404 on the media URL, ffmpeg, a
  transcript that failed validation). Counts against the retry ceiling.
* ``AsrError`` — the service's fault. The caller puts the job back without
  spending an attempt.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..config import DEFAULT_USER_AGENT
from ..downloader import parse_markdown_blocks
from ..logger import get_logger
from ..models import ArticleRecord
from ..normalize_html import normalize_html
from ..storage import PostgresStorage, fetchone_row
from . import archive as archive_module
from . import audio as audio_module
from .asr import AsrClient, client_for
from .validate import check

logger = get_logger(__name__)

#: Ceiling on one download. A 4 h episode at 128 kbps is roughly 230 MB, so this
#: only exists to stop a wrong URL from filling the disk.
MAX_DOWNLOAD_BYTES = 2 * 1024**3

#: Audio is fetched in ranged blocks, which is not cosmetic: googlevideo throttles a
#: plain GET from a flagged exit IP to ~10 KB/s while serving ranged requests at
#: 3-7 MB/s (measured on the production proxy). The block is also the retry unit, so
#: a connection that dies halfway costs one block instead of the whole episode.
DOWNLOAD_BLOCK_BYTES = 8 * 1024**2
#: Attempts per block before the job fails and the retry ceiling takes over.
DOWNLOAD_ATTEMPTS = 3
#: Pause before re-requesting a block that failed, growing with the attempt.
DOWNLOAD_RETRY_BACKOFF_SECONDS = 2.0


def _download_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """The caller's headers, with a browser User-Agent unless they set one.

    What HTTP libraries send by default gets refused: cdn.lizhi.fm answers 403 to
    both ``python-httpx/…`` and ``curl/…`` and serves the same URL to a browser agent
    (measured). Sources that need a specific agent (Bilibili's CDN wants its Referer)
    still override this.
    """
    prepared = dict(headers)
    if not any(key.lower() == 'user-agent' for key in prepared):
        prepared['User-Agent'] = DEFAULT_USER_AGENT
    return prepared

#: How the transcript is split for reading. One 300 s chunk is ~1900 characters,
#: which is a wall of text; grouped sentences of about this size read better and
#: give the reader a timestamp to jump to every ~20 seconds.
TARGET_PARAGRAPH_CHARS = 200

_SENTENCE_ENDERS = '。！？!?；;'


class MediaJobSkipped(RuntimeError):
    """Nothing to transcribe, and not a failure."""


class MediaJobFailure(RuntimeError):
    """This article could not be transcribed."""


@dataclass(frozen=True, slots=True)
class Subtitle:
    """Transcript that arrived ready-made (podcast VTT, CC subtitles)."""

    blocks: list[dict[str, Any]]
    markdown: str


@dataclass(frozen=True, slots=True)
class ResolvedMedia:
    """What a source adapter found for a queued item."""

    #: Direct URL of the audio to download. ``None`` when there is no audio.
    audio_url: str | None = None
    #: Request headers the host needs (Bilibili wants a Referer).
    headers: dict[str, str] = field(default_factory=dict)
    #: Proxy the download must go through (YouTube's CDN is unreachable without one).
    proxy: str | None = None
    duration_seconds: int | None = None
    #: Player block for the reader: an iframe for video, a URL for a podcast.
    embed_block: dict[str, Any] | None = None
    #: Set when the source already has a transcript, so no ASR is needed.
    subtitle: Subtitle | None = None


class MediaResolver(Protocol):
    async def resolve(self, job: Mapping[str, Any]) -> ResolvedMedia: ...


class DirectMediaResolver:
    """The queued URL is already the audio file (podcast enclosures).

    The real adapters arrive with the source commits; keeping a one-line default
    here means the pipeline and its tests do not depend on them.
    """

    async def resolve(self, job: Mapping[str, Any]) -> ResolvedMedia:
        return ResolvedMedia(audio_url=str(job['media_url']))


def split_paragraphs(text: str, duration_seconds: float) -> list[tuple[float, str]]:
    """Cut one chunk's text into paragraphs with an estimated start time.

    The timestamp is interpolated by character position inside the chunk — the
    same approximation the backend's own SRT output uses. It is good to a few
    seconds, and the chunk boundaries themselves stay exact.
    """
    stripped = (text or '').strip()
    if not stripped:
        return []
    sentences: list[str] = []
    current = ''
    for character in stripped:
        current += character
        if character in _SENTENCE_ENDERS:
            sentences.append(current)
            current = ''
    if current.strip():
        sentences.append(current)

    paragraphs: list[str] = []
    buffer = ''
    for sentence in sentences:
        if buffer and len(buffer) + len(sentence) > TARGET_PARAGRAPH_CHARS:
            paragraphs.append(buffer)
            buffer = ''
        buffer += sentence
    if buffer:
        paragraphs.append(buffer)

    total = sum(len(paragraph) for paragraph in paragraphs) or 1
    offsets: list[tuple[float, str]] = []
    cursor = 0
    for paragraph in paragraphs:
        offsets.append((cursor / total * duration_seconds, paragraph.strip()))
        cursor += len(paragraph)
    return offsets


def _mmss(seconds: float) -> str:
    total = max(int(seconds), 0)
    return f'{total // 60:02d}:{total % 60:02d}'


def _text(markdown_blocks: Iterable[tuple[float, str]], *, with_time: bool) -> str:
    parts = []
    for start, paragraph in markdown_blocks:
        parts.append(f'**[{_mmss(start)}]** {paragraph}' if with_time else paragraph)
    return '\n\n'.join(parts).strip()


async def _load_article(storage: PostgresStorage, article_pk: int) -> ArticleRecord:
    row = await fetchone_row(
        storage,
        (
            'SELECT biz, article_id, title, item_show_type, author, digest, cover, link,'
            ' source_url, publish_at, duration_seconds, raw_json FROM articles WHERE id = %s'
        ),
        [article_pk],
    )
    if not row:
        raise MediaJobSkipped(f'文章 {article_pk} 已不存在')
    raw: Any = row.get('raw_json')
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    return ArticleRecord(
        biz=str(row['biz']),
        article_id=str(row['article_id']),
        title=str(row['title']),
        item_show_type=row['item_show_type'],
        author=row['author'],
        digest=row['digest'],
        cover=row['cover'],
        link=str(row['link']),
        source_url=row['source_url'],
        publish_at=row['publish_at'],
        duration_seconds=row['duration_seconds'],
        raw=raw if isinstance(raw, dict) else {},
    )


def _total_bytes(response: httpx.Response, current: int | None) -> int | None:
    """Learn the full size from a response, when it says so.

    A 206 answers with ``content-range: bytes 0-1023/45243468``; a 200 without range
    support only carries ``content-length``, which is the whole body by definition.
    """
    content_range = response.headers.get('content-range') or ''
    if '/' in content_range:
        tail = content_range.rsplit('/', 1)[1].strip()
        if tail.isdigit():
            return int(tail)
    if response.status_code == 200:
        length = response.headers.get('content-length') or ''
        if length.isdigit():
            return int(length)
    return current


async def _download(
    url: str,
    target: Path,
    *,
    headers: Mapping[str, str],
    timeout: float,
    proxy: str | None = None,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> int:
    """Fetch ``url`` into ``target`` in ranged blocks, keeping what arrived.

    A partial file is picked up where it stopped, so a block that fails after 7 MiB
    is re-requested for its last byte rather than for the whole episode.
    """
    written = target.stat().st_size if target.is_file() else 0
    total: int | None = None
    blocks = max_bytes // DOWNLOAD_BLOCK_BYTES + 2
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, proxy=proxy or None) as client:
        for _ in range(blocks):
            appended = 0
            for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
                # 每次尝试都按当前进度重算区间：上一次传到一半断掉时，续的是断点而不是重来。
                request_headers = _download_headers(headers)
                request_headers['Range'] = f'bytes={written}-{written + DOWNLOAD_BLOCK_BYTES - 1}'
                appended = 0
                try:
                    async with client.stream('GET', url, headers=request_headers) as response:
                        if response.status_code == 416:
                            # 请求越界 = 服务器说「没有了」。正常收尾，但长度是否够由
                            # 循环末尾统一判断（服务器先报过 59 分钟、之后只给 2 MB 的
                            # 情况必须当场失败，而不是当完整的存下来）。
                            break
                        if response.status_code not in (200, 206):
                            raise MediaJobFailure(f'音频下载失败：HTTP {response.status_code}')
                        if response.status_code == 200 and written:
                            # 服务器不支持 Range，把半截文件丢掉重来，免得两段接在一起。
                            written = 0
                        total = _total_bytes(response, total)
                        with target.open('ab' if written else 'wb') as handle:
                            async for piece in response.aiter_bytes(1 << 16):
                                written += len(piece)
                                appended += len(piece)
                                if written > max_bytes:
                                    raise MediaJobFailure(f'音频超过 {max_bytes // 1024**3} GiB 上限')
                                handle.write(piece)
                    break
                except MediaJobFailure:
                    raise
                except httpx.HTTPError as exc:
                    if attempt >= DOWNLOAD_ATTEMPTS:
                        raise MediaJobFailure(f'音频下载失败：{exc}') from exc
                    logger.warning('音频第 %d 字节起的分片第 %d 次失败：%s', written, attempt, exc)
                    await asyncio.sleep(DOWNLOAD_RETRY_BACKOFF_SECONDS * attempt)
            if total is not None and written >= total:
                break
            if not appended:
                # 没有进度也问不出总数：再要下去只会是空转。
                break
    if not written:
        raise MediaJobFailure('音频下载得到了空文件')
    if total is not None and written < total:
        # 服务器先报了一个总长，之后却说「没有了」（416）或给了一个空片。曾经因此
        # 把 2 MB 当成完整的 59 分钟节目留了档——这里把短文件当失败，让上层重试。
        raise MediaJobFailure(f'音频被截断：只拿到 {written}/{total} 字节')
    return written



async def _transcribe_chunk(
    transcriber: AsrClient,
    chunk: audio_module.Chunk,
    *,
    settings: Mapping[str, Any],
    workdir: Path,
    max_new_tokens: int,
) -> list[tuple[float, str]]:
    """Transcribe one chunk, retrying once as two halves if the output looks wrong.

    Re-running the same audio would only reproduce the same garbage, so the retry
    changes the input instead: the chunk is cut in half and each half is asked for
    again.
    """
    transcript = await transcriber.transcribe(chunk.path)
    verdict = check(transcript.text, chunk.duration_seconds, max_new_tokens=max_new_tokens)
    if verdict.ok:
        return [
            (chunk.start_seconds + start, text)
            for start, text in split_paragraphs(transcript.text, chunk.duration_seconds)
        ]
    if verdict.silent:
        logger.info('切片 %d 没有内容（%s），跳过', chunk.index, verdict.reason)
        return []

    logger.warning('切片 %d 输出异常（%s），对半重试', chunk.index, verdict.reason)
    half_dir = workdir / f'halves-{chunk.index}'
    halves = await asyncio.to_thread(
        audio_module.split,
        chunk.path,
        half_dir,
        chunk_seconds=max(int(chunk.duration_seconds / 2) or 1, 1),
        min_tail_seconds=0,
        max_chunk_seconds=int(settings['max_chunk_seconds']),
    )
    recovered: list[tuple[float, str]] = []
    for half in halves:
        half_transcript = await transcriber.transcribe(half.path)
        half_verdict = check(half_transcript.text, half.duration_seconds, max_new_tokens=max_new_tokens)
        if half_verdict.silent:
            continue
        if not half_verdict.ok:
            raise MediaJobFailure(f'切片 {chunk.index} 转写失败：{half_verdict.reason}')
        recovered.extend(
            (chunk.start_seconds + half.start_seconds + start, text)
            for start, text in split_paragraphs(half_transcript.text, half.duration_seconds)
        )
    if not recovered:
        raise MediaJobFailure(f'切片 {chunk.index} 转写失败：{verdict.reason}')
    return recovered


def _show_notes(article: ArticleRecord) -> tuple[list[dict[str, Any]], str]:
    """The article's own text as blocks: a podcast's show notes, which the feed gave us.

    The digest already holds them (the listing stores the feed's description), so
    nothing has to travel through the job payload. They go above the transcript — it
    is what the episode says about itself, and the transcript's timestamps mark where
    it ends
    """
    html = str(article.digest or '')
    if '<' not in html:
        return [], ''
    markdown = normalize_html(html, 'markdown').strip()
    if not markdown:
        return [], ''
    _, _, blocks, _ = parse_markdown_blocks(markdown)
    return blocks, markdown


async def _progress(storage: PostgresStorage, job_id: int, *, stage: str, progress: int) -> None:
    """Record where a job is, and commit it.

    The worker commits when the job is done, which for an hour-long episode means the
    reader's "转写中" line shows nothing at all for that hour.
    """
    await storage.media_jobs.update_progress(job_id, stage=stage, progress=progress)
    await storage.commit()


def _media_blocks(
    resolved: ResolvedMedia,
    *,
    article_pk: int,
    archived: bool,
) -> list[dict[str, Any]]:
    """The players that go above the text: the video, and the audio we kept.

    Both, when both exist — a video is watchable, and the archived audio is what
    survives the original disappearing (and what makes a transcript anchor seek).
    ``_save`` decides which of them the reader actually gets to see.

    A resolved audio url with no archive (keep_media off) is still worth a player,
    because it streams from wherever the source hosts it.
    """
    blocks: list[dict[str, Any]] = []
    if resolved.embed_block:
        blocks.append(resolved.embed_block)
    if archived:
        blocks.append({'type': 'audio', 'url': f'/api/media/{article_pk}/audio'})
    elif resolved.audio_url and not resolved.embed_block and not resolved.headers:
        # 直接指向源站的播放器。带自定义头的源（B站 CDN 要 Referer）不能这样给：
        # 浏览器的 <audio> 加不了那些头，给出去也是个播不响的播放器。
        blocks.append({'type': 'audio', 'url': str(resolved.audio_url)})
    return blocks


async def _save(
    storage: PostgresStorage,
    article: ArticleRecord,
    *,
    blocks: list[dict[str, Any]],
    markdown: str,
) -> int:
    """Write the transcript as the article body, leaving images alone."""
    return await storage.articles.save_article_content(
        article,
        url_token=None,
        title=article.title,
        item_show_type=article.item_show_type,
        content_markdown=markdown,
        content_blocks=blocks,
        cover_url=None,
        images=[],
        replace_images=False,
    )


def _paragraph_blocks(paragraphs: Iterable[tuple[float, str]]) -> list[dict[str, Any]]:
    return [
        {'type': 'paragraph', 'text': paragraph, 't': round(start, 1)} for start, paragraph in paragraphs if paragraph
    ]


async def _save_subtitle(
    storage: PostgresStorage,
    *,
    job_id: int,
    article: ArticleRecord,
    article_pk: int,
    resolved: ResolvedMedia,
    archived: bool,
) -> dict[str, Any]:
    """Write a ready-made subtitle in as the body, with the player above it."""
    await _progress(storage, job_id, stage='subtitle', progress=90)
    show_notes, notes_markdown = _show_notes(article)
    blocks = _media_blocks(resolved, article_pk=article_pk, archived=archived)
    blocks.extend(show_notes)
    blocks.extend(resolved.subtitle.blocks)  # type: ignore[union-attr]
    markdown = '\n\n'.join(part for part in (notes_markdown, resolved.subtitle.markdown) if part)  # type: ignore[union-attr]
    await _save(storage, article, blocks=blocks, markdown=markdown)
    return {
        'article_pk': article_pk,
        'segments': len(resolved.subtitle.blocks),  # type: ignore[union-attr]
        'subtitle': True,
        'archived': archived,
    }


async def transcribe_job(
    *,
    storage: PostgresStorage,
    job: Mapping[str, Any],
    settings: Mapping[str, Any],
    resolver: MediaResolver,
    transcriber: AsrClient | None = None,
) -> dict[str, Any]:
    """Run one job. Raises the exceptions documented at the top of the module."""
    article_pk = int(job['article_pk'])
    article = await _load_article(storage, article_pk)
    resolved = await resolver.resolve(job)
    subtitle = resolved.subtitle
    job_id = int(job['id'])
    ceiling = float(settings.get('max_duration_seconds') or 0)

    async def keep_subtitle(*, archived: bool) -> dict[str, Any]:
        return await _save_subtitle(
            storage,
            job_id=job_id,
            article=article,
            article_pk=article_pk,
            resolved=resolved,
            archived=archived,
        )

    # 列表阶段就知道时长的，先拦下来，不必白下几十兆。字幕是平台给的，超长也照样用。
    known = float(resolved.duration_seconds or 0)
    if ceiling and known > ceiling:
        if subtitle is None:
            raise MediaJobSkipped(f'音频 {known / 60:.0f} 分钟，超过 {ceiling / 60:.0f} 分钟上限')
        return await keep_subtitle(archived=False)

    if not resolved.audio_url:
        if subtitle is None:
            raise MediaJobSkipped('这个条目没有可下载的音频')
        return await keep_subtitle(archived=False)

    tier = str(settings.get('asr_tier') or 'accurate')
    transcriber = transcriber or client_for(dict(settings), tier)
    max_new_tokens = int(settings.get('asr_max_new_tokens') or 2048)

    with tempfile.TemporaryDirectory(prefix=f'hippo-media-{article_pk}-') as temp_name:
        workdir = Path(temp_name)
        source = workdir / 'source.bin'
        await _progress(storage, job_id, stage='download', progress=5)
        # 已经存在对象存储里的视频优先：那是别人手上就有的文件，不必再从网上下一遍。
        stored = await storage.media_assets.get(article_pk, kind='video')
        if stored and stored.get('state') == 'stored' and stored.get('s3_key'):
            logger.info('用留档视频作为源（article_pk=%s，%s 字节）', article_pk, stored.get('size_bytes'))
            size = await asyncio.to_thread(archive_module.fetch_object, str(stored['s3_key']), source)
        else:
            size = await _download(
                str(resolved.audio_url),
                source,
                headers=resolved.headers,
                proxy=resolved.proxy,
                timeout=float(settings.get('asr_timeout_seconds') or 900),
            )
        duration = float(resolved.duration_seconds or await asyncio.to_thread(audio_module.probe_duration, source))
        over_ceiling = bool(ceiling) and duration > ceiling
        if over_ceiling and subtitle is None:
            raise MediaJobSkipped(f'音频 {duration / 60:.0f} 分钟，超过 {ceiling / 60:.0f} 分钟上限')

        # 有字幕也要留档：字幕是平台给的，音频是这个条目里我们唯一能留下来的东西。
        archived = None if over_ceiling else await _archive(storage, article_pk, source, job=job, settings=settings)

        if subtitle is not None:
            # 有现成字幕就不花 ASR 的钱，但下载、转码、留档照做。
            # `bool`：留档失败时返回的是 False，写 `is not None` 会给出一个指向
            # 不存在对象的播放器。
            return await keep_subtitle(archived=bool(archived))

        await _progress(storage, job_id, stage='transcribe', progress=25)

        chunks = await asyncio.to_thread(
            audio_module.prepare,
            source,
            workdir,
            chunk_seconds=int(settings['chunk_seconds']),
            min_tail_seconds=int(settings['min_tail_seconds']),
            max_chunk_seconds=int(settings['max_chunk_seconds']),
        )
        paragraphs: list[tuple[float, str]] = []
        for done, chunk in enumerate(chunks, start=1):
            progress = 25 + int(done / len(chunks) * 65)
            await _progress(storage, job_id, stage='transcribe', progress=progress)
            paragraphs.extend(
                await _transcribe_chunk(
                    transcriber,
                    chunk,
                    settings=settings,
                    workdir=workdir,
                    max_new_tokens=max_new_tokens,
                )
            )
        if not paragraphs:
            raise MediaJobSkipped('整段音频都没有转写到内容')

        show_notes, notes_markdown = _show_notes(article)
        blocks = _media_blocks(resolved, article_pk=article_pk, archived=archived)
        blocks.extend(show_notes)
        blocks.extend(_paragraph_blocks(paragraphs))

        await _progress(storage, job_id, stage='save', progress=95)
        transcript = _text(paragraphs, with_time=False)
        markdown = '\n\n'.join(part for part in (notes_markdown, transcript) if part)
        await _save(storage, article, blocks=blocks, markdown=markdown)
        return {
            'article_pk': article_pk,
            'segments': len(paragraphs),
            'duration_seconds': round(duration, 1),
            'bytes': size,
            'archived': archived,
        }


async def _archive(
    storage: PostgresStorage,
    article_pk: int,
    source: Path,
    *,
    job: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> bool:
    """Keep a listenable copy. A failure here never fails the transcription."""
    if not settings.get('keep_media'):
        return False
    bitrate = str(settings.get('keep_media_bitrate') or '128k')
    target = source.with_name(f'archive-{source.stem}.mp3')
    try:
        await asyncio.to_thread(audio_module.archive, source, target, bitrate=bitrate)
        await storage.media_jobs.update_progress(int(job['id']), stage='archive', progress=20)
        key, size = await asyncio.to_thread(
            archive_module.store_media,
            article_pk=article_pk,
            path=target,
        )
    except Exception as exc:
        logger.warning('留档音频失败（article_pk=%s）：%s', article_pk, exc)
        await storage.media_assets.mark_failed(article_pk, error=str(exc))
        return False
    await storage.media_assets.save(
        article_pk=article_pk,
        orig_url=str(job.get('media_url') or ''),
        content_type=archive_module.DEFAULT_CONTENT_TYPE,
        size_bytes=size,
        duration_seconds=None,
        s3_key=key,
    )
    return True


__all__ = [
    'DOWNLOAD_ATTEMPTS',
    'DOWNLOAD_BLOCK_BYTES',
    'MAX_DOWNLOAD_BYTES',
    'TARGET_PARAGRAPH_CHARS',
    'DirectMediaResolver',
    'MediaJobFailure',
    'MediaJobSkipped',
    'MediaResolver',
    'ResolvedMedia',
    'Subtitle',
    'split_paragraphs',
    'transcribe_job',
]
