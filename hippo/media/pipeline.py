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
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..config import DEFAULT_USER_AGENT
from ..logger import get_logger
from ..models import ArticleRecord
from ..storage import PostgresStorage, fetchone_row
from . import archive as archive_module
from . import audio as audio_module
from . import video as video_module
from .asr import AsrClient, client_for
from .validate import check

logger = get_logger(__name__)

#: Ceiling on everything one job downloads. A 4 h episode at 128 kbps is roughly 230 MB,
#: so this only exists to stop a wrong URL from filling the disk. One job's tracks share
#: it (``_download``'s ``start_offset``), because splitting a video into two files must
#: not hand the second one a fresh budget.
MAX_DOWNLOAD_BYTES = 2 * 1024**3

#: How the video asset is named in ``article_media`` and in the object store. The
#: phase-1 contract fixed both strings.
VIDEO_KIND = 'video'
VIDEO_CONTENT_TYPE = 'video/mp4'

#: Media files are fetched in ranged blocks, which is not cosmetic: googlevideo throttles a
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

#: Hesitation sounds. The model writes one every time the speaker pauses, and none
#: of these characters is ever part of a Chinese word, so they go wherever they are.
_HESITATIONS = '嗯呃唔'

#: Clause-final particles: "是啊，", "成分股啊，". They also carry meaning after a
#: one-character exclamation — "天啊", "好呀" — and jieba splits that into 天 + 啊,
#: so the position alone does not tell a filler from a word: one is only dropped
#: when the clause in front of it holds real content (or nothing at all, as in the
#: "啊，这个确实是" that opens a sentence).
_CLAUSE_PARTICLES = '啊哦噢呀'

_HESITATION_RE = re.compile(f'[{_HESITATIONS}]+')
_CLAUSE_PARTICLE_RE = re.compile(rf'([^\s，。！？、；：,.!?;:]*)([{_CLAUSE_PARTICLES}]+)(?=[\s，。！？、；：,.!?;:]|$)')


def _drop_clause_particle(match: re.Match[str]) -> str:
    clause, particle = match.group(1), match.group(2)
    return clause if len(clause) != 1 else clause + particle


def clean_transcript(text: str) -> str:
    """Strip the interjections an ASR model fills pauses with.

    Runs before the text is cut into paragraphs, so the interpolated timestamps are
    measured against the words that are actually shown — a filler has no duration of
    its own worth preserving.
    """
    cleaned = _HESITATION_RE.sub('', text or '')
    cleaned = _CLAUSE_PARTICLE_RE.sub(_drop_clause_particle, cleaned)
    # Dropping an interjection leaves its punctuation behind: "。 啊，这个" would read
    # "。 ，这个", and "嗯。呃。" a bare "。。". Neither says anything, so both go.
    cleaned = re.sub(r'(?<=[。！？!?；;])[。！？!?；;]+', '', cleaned)
    cleaned = re.sub(r'(?<=[。！？!?；;])\s*[，、,]\s*', '', cleaned)
    cleaned = re.sub(rf'^\s*[{_SENTENCE_ENDERS}，、；：,;:]+\s*', '', cleaned)
    return re.sub(r'\s+', ' ', cleaned).strip()


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
    #: Direct HTTP(S) URL of a progressive MP4/H.264 video the source resolved.
    #: ``None`` when the source has no compatible track; never an HLS/DASH manifest.
    video_url: str | None = None
    #: Whether that progressive MP4 already carries its AAC audio. ``False`` means the
    #: picture has to be muxed with ``audio_url`` before it is watchable.
    video_has_audio: bool = False
    #: Request headers the host needs (Bilibili wants a Referer).
    headers: dict[str, str] = field(default_factory=dict)
    #: Proxy the download must go through (YouTube's CDN is unreachable without one).
    proxy: str | None = None
    duration_seconds: int | None = None
    #: The source's own player block. Video sources use it as metadata (the pipeline
    #: drops ``embed_url`` and serves its own file); a podcast URL is still usable.
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

    This is the ASR path, so the interjections the model writes into every pause are
    cleaned out first. A ready-made subtitle keeps its wording: its timestamps are
    real, and its cues are what the uploader chose to publish.
    """
    stripped = clean_transcript(text)
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


def _range_bounds(response: httpx.Response) -> tuple[int, int | None]:
    """The ``start`` and ``total`` a 206's ``Content-Range`` declares.

    A block is only usable where it was asked for, so the start has to be readable
    rather than assumed: a host that answers with someone else's range would have us
    splice two unrelated pieces into one file.
    """
    header = response.headers.get('content-range') or ''
    bounds, _, raw_total = header.partition('/')
    value = bounds.strip()
    if not value.startswith('bytes '):
        raise MediaJobFailure('media server sent an unparsable Content-Range')
    raw_start, _, raw_end = value[len('bytes ') :].partition('-')
    if not raw_start.isdigit() or not raw_end.isdigit():
        raise MediaJobFailure('media server sent an unparsable Content-Range')
    start = int(raw_start)
    end = int(raw_end)
    if end < start:
        # end < start is not a range; it is a claim that cannot be spliced in.
        raise MediaJobFailure(f'media server sent a reversed Content-Range: {start}-{end}')
    total = int(raw_total.strip()) if raw_total.strip().isdigit() else None
    return start, total


async def _download(
    url: str,
    target: Path,
    *,
    headers: Mapping[str, str],
    timeout: float,
    proxy: str | None = None,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    start_offset: int = 0,
) -> int:
    """Fetch ``url`` into ``target`` in ranged blocks, keeping what arrived.

    A partial file is picked up where it stopped, so a block that fails after 7 MiB
    is re-requested for its last byte rather than for the whole episode.

    ``start_offset`` is what earlier tracks of the same job already spent: the ceiling
    is per job, so a second track must not get a fresh 2 GiB of its own. The budget is
    recomputed as ``start_offset + written`` on every piece instead of being
    accumulated, because a server that ignores ``Range`` rewinds ``written`` to 0 and
    the bytes it already sent must not stay charged twice.

    ``timeout`` bounds the whole download — every block, retry and backoff — rather
    than one piece of it, so a host that trickles forever still ends in a failure.
    """
    prior = max(int(start_offset), 0)
    if prior >= max_bytes:
        raise MediaJobFailure(f'media exceeds the {max_bytes} byte ceiling')
    written = target.stat().st_size if target.is_file() else 0
    total: int | None = None
    blocks = max_bytes // DOWNLOAD_BLOCK_BYTES + 2
    try:
        async with asyncio.timeout(timeout):
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
                                    raise MediaJobFailure(f'media download failed: HTTP {response.status_code}')
                                if response.status_code == 206:
                                    start, range_total = _range_bounds(response)
                                    if start != written:
                                        raise MediaJobFailure(
                                            f'media server answered from byte {start}, expected {written}'
                                        )
                                    if range_total is not None:
                                        total = range_total
                                else:
                                    if written:
                                        # 服务器不支持 Range，把半截文件丢掉重来，免得两段接在一起。
                                        written = 0
                                    total = _total_bytes(response, total)
                                # 总长已经报出来的，不必先下几百兆再说超限。
                                if total is not None and prior + total > max_bytes:
                                    raise MediaJobFailure(f'media exceeds the {max_bytes} byte ceiling')
                                with target.open('ab' if written else 'wb') as handle:
                                    async for piece in response.aiter_bytes(1 << 16):
                                        written += len(piece)
                                        appended += len(piece)
                                        if prior + written > max_bytes:
                                            raise MediaJobFailure(f'media exceeds the {max_bytes} byte ceiling')
                                        handle.write(piece)
                            break
                        except MediaJobFailure:
                            raise
                        except httpx.HTTPError as exc:
                            if attempt >= DOWNLOAD_ATTEMPTS:
                                raise MediaJobFailure(f'media download failed: {exc}') from exc
                            logger.warning('媒体第 %d 字节起的分片第 %d 次失败：%s', written, attempt, exc)
                            await asyncio.sleep(DOWNLOAD_RETRY_BACKOFF_SECONDS * attempt)
                    if total is not None and written >= total:
                        break
                    if not appended:
                        # 没有进度也问不出总数：再要下去只会是空转。
                        break
    except TimeoutError as exc:
        raise MediaJobFailure(f'media download did not finish within {timeout:.0f}s') from exc
    if not written:
        raise MediaJobFailure('media download produced an empty file')
    if total is not None and written < total:
        # The server announced a total, then said "no more" (416) or sent a short piece.
        # That once stored 2 MB as a complete 59-minute episode; a short file is a failure
        # so the queue retries, never a file that looks whole.
        raise MediaJobFailure(f'media truncated: received {written} of {total} bytes')
    return written


def _checked_size(path: Path, *, label: str, max_bytes: int) -> int:
    """Non-empty and inside the ceiling, for a file about to be read or stored.

    ``_download`` checks what it fetched, but a merged file and a stored object reach
    the store without passing through it, so the property is asserted once more here
    instead of being trusted to the producer.
    """
    size = path.stat().st_size if path.is_file() else 0
    if not size:
        raise MediaJobFailure(f'{label} is an empty file')
    if size > max_bytes:
        raise MediaJobFailure(f'{label} exceeds the {max_bytes} byte ceiling')
    return size


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
    it ends.

    The digest is the source's plain-text contract, so every non-empty line becomes a
    paragraph exactly as written. It is never parsed again as HTML here: text that
    merely looks like a tag (``Use <label> literally``) is content, not markup, and
    guessing would delete it.
    """
    lines = [line.strip() for line in str(article.digest or '').splitlines() if line.strip()]
    return [{'type': 'paragraph', 'text': line} for line in lines], '\n'.join(lines)


async def _progress(storage: PostgresStorage, job_id: int, *, stage: str, progress: int) -> None:
    """Record where a job is, and commit it.

    The worker commits when the job is done, which for an hour-long episode means the
    reader's "转写中" line shows nothing at all for that hour.
    """
    await storage.media_jobs.update_progress(job_id, stage=stage, progress=progress)
    await storage.commit()


def _video_block(resolved: ResolvedMedia, *, article_pk: int) -> dict[str, Any]:
    """The native player for the video Hippo keeps.

    The provider/video_id/cover metadata comes from the source's embed block; its
    ``embed_url`` is dropped, because the player is ours and the file is ours.
    """
    embed = resolved.embed_block or {}
    block: dict[str, Any] = {'type': 'video', 'url': f'/api/media/{article_pk}/{VIDEO_KIND}'}
    for key in ('provider', 'video_id', 'cover_url'):
        value = embed.get(key)
        if value:
            block[key] = value
    return block


def _media_blocks(
    resolved: ResolvedMedia,
    *,
    article_pk: int,
    audio_archived: bool,
    video_stored: bool,
) -> list[dict[str, Any]]:
    """The players that go above the text: the video we keep, and the audio.

    ``video_stored`` is passed in rather than derived here: the URL is only usable
    once the object is really in the store, and only ``transcribe_job`` knows that.
    A source's own video embed is never handed out — Hippo plays its own copy, and
    an iframe would put the platform back in the reader's path.
    """
    blocks: list[dict[str, Any]] = []
    embed = resolved.embed_block
    if embed and embed.get('type') != 'video':
        # 播客的播放器不是 iframe，照旧给出去。
        blocks.append(embed)
    if video_stored:
        blocks.append(_video_block(resolved, article_pk=article_pk))
    if audio_archived:
        blocks.append({'type': 'audio', 'url': f'/api/media/{article_pk}/audio'})
    elif resolved.audio_url and not embed and not resolved.video_url and not resolved.headers:
        # 直接指向源站的播放器。带自定义头的源（B站 CDN 要 Referer）不能这样给：
        # 浏览器的 <audio> 加不了那些头，给出去也是个播不响的播放器。
        blocks.append({'type': 'audio', 'url': str(resolved.audio_url)})
    return blocks


async def _store_video(
    storage: PostgresStorage,
    article_pk: int,
    path: Path,
    *,
    job: Mapping[str, Any],
    duration_seconds: float,
) -> None:
    """Upload the watchable MP4, or make the job fail trying.

    Unlike the audio copy — which is a bonus next to the transcript — the video *is*
    the article, so a failure marks the video failed and raises instead of letting
    the job finish with a player that would point at nothing.
    """
    try:
        await storage.media_jobs.update_progress(int(job['id']), stage='archive', progress=15)
        key, size = await asyncio.to_thread(
            archive_module.store_media,
            article_pk=article_pk,
            path=path,
            kind=VIDEO_KIND,
            content_type=VIDEO_CONTENT_TYPE,
        )
    except Exception as exc:
        logger.warning('留档视频失败（article_pk=%s）：%s', article_pk, exc)
        await storage.media_assets.mark_failed(article_pk, kind=VIDEO_KIND, error=str(exc))
        raise MediaJobFailure(f'视频留档失败：{exc}') from exc
    await storage.media_assets.save(
        article_pk=article_pk,
        kind=VIDEO_KIND,
        orig_url=str(job.get('media_url') or ''),
        content_type=VIDEO_CONTENT_TYPE,
        size_bytes=size,
        duration_seconds=int(duration_seconds) if duration_seconds else None,
        s3_key=key,
    )


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
    video_kept: bool,
) -> dict[str, Any]:
    """Write a ready-made subtitle in as the body, with the player above it."""
    await _progress(storage, job_id, stage='subtitle', progress=90)
    show_notes, notes_markdown = _show_notes(article)
    blocks = _media_blocks(
        resolved,
        article_pk=article_pk,
        audio_archived=archived,
        video_stored=video_kept,
    )
    blocks.extend(show_notes)
    blocks.extend(resolved.subtitle.blocks)  # type: ignore[union-attr]
    markdown = '\n\n'.join(part for part in (notes_markdown, resolved.subtitle.markdown) if part)  # type: ignore[union-attr]
    await _save(storage, article, blocks=blocks, markdown=markdown)
    return {
        'article_pk': article_pk,
        'segments': len(resolved.subtitle.blocks),  # type: ignore[union-attr]
        'subtitle': True,
        'archived': archived,
        'video_stored': video_kept,
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

    async def keep_subtitle(*, archived: bool, video_kept: bool = False) -> dict[str, Any]:
        return await _save_subtitle(
            storage,
            job_id=job_id,
            article=article,
            article_pk=article_pk,
            resolved=resolved,
            archived=archived,
            video_kept=video_kept,
        )

    # 已经存在对象存储里的视频优先：那是别人手上就有的文件，不必再从网上下一遍，
    # 也不必再传一次。时长闸门之前就看它，因为「不下载」不等于「没得看」。
    stored = await storage.media_assets.get(article_pk, kind=VIDEO_KIND)
    reused = bool(stored and stored.get('state') == 'stored' and stored.get('s3_key'))

    # 列表阶段就知道时长的，先拦下来，不必白下几十兆。字幕是平台给的，超长也照样用。
    known = float(resolved.duration_seconds or 0)
    if ceiling and known > ceiling:
        if subtitle is None:
            raise MediaJobSkipped(f'media is {known / 60:.0f} minutes, over the {ceiling / 60:.0f} minute ceiling')
        return await keep_subtitle(archived=False, video_kept=reused)

    has_video = bool(resolved.video_url)
    keep_media = bool(settings.get('keep_media'))
    # A ready-made subtitle needs no ASR at all, and the client is built as late as
    # possible: a job that a duration gate or a missing track skips must not open a
    # connection it will never use.
    needs_asr = subtitle is None
    # 已经存在库里的视频可以自己当 ASR 的输入，不需要 resolver 再给一个音频轨。
    has_audio_source = bool(resolved.audio_url) or (reused and needs_asr)
    if not has_audio_source and not has_video:
        if subtitle is None:
            raise MediaJobSkipped('this entry has no downloadable audio or video')
        return await keep_subtitle(archived=False, video_kept=reused)
    if subtitle is not None and not keep_media and not reused:
        # 字幕已在手上，媒体又明确不要留：下载只会白花带宽和磁盘。
        return await keep_subtitle(archived=False)

    max_new_tokens = int(settings.get('asr_max_new_tokens') or 2048)
    timeout = float(settings.get('asr_timeout_seconds') or 900)

    with tempfile.TemporaryDirectory(prefix=f'hippo-media-{article_pk}-') as temp_name:
        workdir = Path(temp_name)
        await _progress(storage, job_id, stage='download', progress=5)
        if reused and subtitle is not None:
            # 现成字幕 + 已经在库里的视频：两样都不必再动。
            return await keep_subtitle(archived=False, video_kept=True)

        video_path: Path | None = None
        video_kept = reused
        stored_bytes = 0
        source: Path | None
        audio_source: Path | None = None
        if reused:
            logger.info('用留档视频作为源（article_pk=%s，%s 字节）', article_pk, stored.get('size_bytes'))
            source = workdir / 'source.bin'
            try:
                stored_bytes = await asyncio.to_thread(
                    archive_module.fetch_object,
                    str(stored['s3_key']),
                    source,
                    max_bytes=MAX_DOWNLOAD_BYTES,
                )
            except archive_module.ArchiveError as exc:
                # Same rule as a failed merge: an unreadable archive is retryable, not a
                # silent success built on a source file that does not exist.
                raise MediaJobFailure(f'could not read the archived video: {exc}') from exc
        else:
            # 视频轨只在我们打算留下它、或它是唯一的转写来源时才下。
            audio_path: Path | None = None
            bytes_used = 0
            if has_video and (keep_media or not resolved.audio_url):
                video_path = workdir / 'video.mp4'
                bytes_used += await _download(
                    str(resolved.video_url),
                    video_path,
                    headers=resolved.headers,
                    proxy=resolved.proxy,
                    timeout=timeout,
                    start_offset=bytes_used,
                )
            if resolved.audio_url and (video_path is None or not resolved.video_has_audio):
                audio_path = workdir / 'audio.bin'
                # 分轨共享同一个 2 GiB 预算：分轨不是每轨各给一份。
                bytes_used += await _download(
                    str(resolved.audio_url),
                    audio_path,
                    headers=resolved.headers,
                    proxy=resolved.proxy,
                    timeout=timeout,
                    start_offset=bytes_used,
                )
                # 只有真正的音频文件才能进 `_archive`：`original` 是原样拷贝，把
                # 一个 MP4 改成 .mp3 存成 audio/mpeg 就是在对读者撒谎。
                audio_source = audio_path
            if video_path is not None and not resolved.video_has_audio:
                if audio_path is None:
                    raise MediaJobFailure('the video has no audio track and none could be merged in')
                merged = workdir / 'merged.mp4'
                try:
                    await asyncio.to_thread(
                        video_module.merge,
                        video_path,
                        audio_path,
                        merged,
                        max_bytes=MAX_DOWNLOAD_BYTES,
                    )
                except video_module.VideoError as exc:
                    # A merge that cannot produce a watchable file is this article's
                    # failure: it spends an attempt instead of becoming a fake success.
                    raise MediaJobFailure(f'video merge failed: {exc}') from exc
                video_path = merged
            source = video_path if video_path is not None else audio_path
            if source is None:
                raise MediaJobSkipped('this entry has no downloadable audio or video')

        duration = float(resolved.duration_seconds or await asyncio.to_thread(audio_module.probe_duration, source))
        over_ceiling = bool(ceiling) and duration > ceiling
        if over_ceiling and subtitle is None:
            raise MediaJobSkipped(f'media is {duration / 60:.0f} minutes, over the {ceiling / 60:.0f} minute ceiling')

        if video_path is not None and keep_media and not over_ceiling:
            # 合并后的文件也得非空且在限额内，不因为下载被 fake 过就跳过这个校验。
            _checked_size(video_path, label='video', max_bytes=MAX_DOWNLOAD_BYTES)
            await _store_video(storage, article_pk, video_path, job=job, duration_seconds=duration)
            video_kept = True
        elif not reused:
            _checked_size(source, label='media', max_bytes=MAX_DOWNLOAD_BYTES)

        # 有字幕也要留档：字幕是平台给的，媒体是这个条目里我们唯一能留下来的东西。
        # 视频已经把声音留下了，就不必再存一份重复的音频。
        if over_ceiling or video_kept or audio_source is None:
            archived = None
        else:
            archived = await _archive(storage, article_pk, audio_source, job=job, settings=settings)

        if subtitle is not None:
            # 有现成字幕就不花 ASR 的钱，但下载、合并、留档照做。
            # `bool`：留档失败时返回的是 False，写 `is not None` 会给出一个指向
            # 不存在对象的播放器。
            return await keep_subtitle(archived=bool(archived), video_kept=video_kept)

        # The client is built here, at the point the ASR is really entered: a job that a
        # subtitle, a duration gate or a download failure stopped must never open a
        # connection. An injected client belongs to the caller and is never closed here;
        # one this function built is always closed, on success and on every failure.
        owned = transcriber is None
        if owned:
            transcriber = client_for(dict(settings), str(settings.get('asr_tier') or 'accurate'))
        try:
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
                raise MediaJobSkipped('the whole recording transcribed to nothing')

            show_notes, notes_markdown = _show_notes(article)
            blocks = _media_blocks(
                resolved,
                article_pk=article_pk,
                audio_archived=archived,
                video_stored=video_kept,
            )
            blocks.extend(show_notes)
            blocks.extend(_paragraph_blocks(paragraphs))

            await _progress(storage, job_id, stage='save', progress=95)
            transcript = _text(paragraphs, with_time=False)
            markdown = '\n\n'.join(part for part in (notes_markdown, transcript) if part)
            await _save(storage, article, blocks=blocks, markdown=markdown)
        finally:
            if owned:
                await _close_transcriber(transcriber)
        return {
            'article_pk': article_pk,
            'segments': len(paragraphs),
            'duration_seconds': round(duration, 1),
            'bytes': stored_bytes or bytes_used,
            'archived': archived,
            'video_stored': video_kept,
        }


async def _close_transcriber(transcriber: AsrClient) -> None:
    """Release a client this module built, without masking why the job ended.

    A failing ``aclose`` is a connection-pool problem, not the article's: the caller
    already holds the real error (or the real result), and a close error must not
    replace it.
    """
    try:
        await transcriber.aclose()
    except Exception:
        logger.warning('failed to close the ASR client')


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
    'VIDEO_CONTENT_TYPE',
    'VIDEO_KIND',
    'DirectMediaResolver',
    'MediaJobFailure',
    'MediaJobSkipped',
    'MediaResolver',
    'ResolvedMedia',
    'Subtitle',
    'clean_transcript',
    'split_paragraphs',
    'transcribe_job',
]
