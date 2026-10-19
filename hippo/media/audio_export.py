"""Lifting an audio track out of a video we already keep.

Video articles used to be archived as the MP4 alone, so their body has a player
and nothing to listen to. Rather than re-downloading a hundred megabytes to save
twenty, the extraction works on the copy in the object store: fetch it, strip the
picture, store the mp3 next to it.

This is a second-best path on purpose. A new transcription now keeps both from
the source it already downloaded, and only the older articles pay for one more
copy of the same bytes through the store.

At most one extraction runs at a time, in a thread: ffmpeg is CPU-bound, and ten
readers pressing the same button must not start ten of them.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from ..logger import get_logger
from ..storage import open_storage
from . import archive as archive_module
from . import audio as audio_module

logger = get_logger(__name__)

#: Bitrate of the extracted copy. Matches the archive default: this file exists to
#: be listened to, not to be a master.
EXPORT_BITRATE = '128k'

#: One extraction at a time. The reader waits for it, so the queue stays short.
_SEMAPHORE: asyncio.Semaphore | None = None
_running: dict[int, asyncio.Task[None]] = {}
_lock: asyncio.Lock | None = None


def _limiter() -> asyncio.Semaphore:
    """The process-wide semaphore, created on the loop that first uses it."""
    global _SEMAPHORE
    if _SEMAPHORE is None:
        _SEMAPHORE = asyncio.Semaphore(1)
    return _SEMAPHORE


def _mutex() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def audio_state(storage, article_pk: int) -> str:
    """Where this article's listenable copy stands, without starting anything.

    ``stored``  — the object is there and the player can use it.
    ``pending`` — an extraction is running.
    ``missing`` — an archived video exists, so a request can produce the audio.
    ``failed``  — the last attempt could not finish; a request retries it.
    ``unavailable`` — there is no archived video to lift the audio out of.
    """
    asset = await storage.media_assets.get(article_pk, kind='audio')
    if asset and asset.get('state') == 'stored' and str(asset.get('s3_key') or '').strip():
        return 'stored'
    # A pending row whose task this process does not hold was interrupted by a
    # restart. There is nothing left to wait for, so it is retried on request.
    if asset and asset.get('state') == 'pending' and article_pk in _running:
        return 'pending'
    video = await storage.media_assets.get(article_pk, kind=archive_module.VIDEO_KIND)
    if not video or video.get('state') != 'stored' or not str(video.get('s3_key') or '').strip():
        return 'unavailable'
    return 'failed' if asset and asset.get('state') == 'failed' else 'missing'


async def ensure_audio(storage, article_pk: int) -> str:
    """Start an extraction if one is needed, and report the state it left.

    Starting twice is not an error: the second request joins the task already in
    flight instead of queueing another copy of the same work.
    """
    state = await audio_state(storage, article_pk)
    if state in ('stored', 'unavailable', 'pending'):
        return state
    video = await storage.media_assets.get(article_pk, kind=archive_module.VIDEO_KIND)
    if not video or not str(video.get('s3_key') or '').strip():
        return 'unavailable'
    async with _mutex():
        if article_pk in _running:
            return 'pending'
        # The row is claimed first: a request arriving while this task runs must see
        # `pending`, not the `failed`/`missing` it was left with.
        await storage.media_assets.mark_pending(article_pk)
        await storage.commit()
        task = asyncio.create_task(_extract(article_pk, str(video['s3_key'])))
        _running[article_pk] = task
        task.add_done_callback(lambda _task: _running.pop(article_pk, None))
    return 'pending'


async def _extract(article_pk: int, video_key: str) -> None:
    """Fetch, strip, store. Runs detached, so every failure is reported in the log."""
    async with _limiter():
        try:
            async with open_storage() as storage:
                await _extract_with(storage, article_pk, video_key)
        except Exception as exc:
            logger.warning('抽取音频失败（article_pk=%s）：%s', article_pk, exc)


async def _extract_with(storage, article_pk: int, video_key: str) -> None:
    with tempfile.TemporaryDirectory(prefix=f'hippo-audio-{article_pk}-') as temp_name:
        workdir = Path(temp_name)
        source = workdir / 'video.mp4'
        target = workdir / 'audio.mp3'
        try:
            await asyncio.to_thread(archive_module.fetch_object, video_key, source)
            await asyncio.to_thread(audio_module.archive, source, target, bitrate=EXPORT_BITRATE)
            key, size = await asyncio.to_thread(
                archive_module.store_media,
                article_pk=article_pk,
                path=target,
            )
        except Exception as exc:
            await storage.media_assets.mark_failed(article_pk, error=str(exc))
            await storage.commit()
            raise
        # `orig_url` stays empty: this file has no source URL of its own, and a
        # redirect to the article's page would hand the browser HTML as audio.
        await storage.media_assets.save(
            article_pk=article_pk,
            orig_url=None,
            content_type=archive_module.DEFAULT_CONTENT_TYPE,
            size_bytes=size,
            duration_seconds=None,
            s3_key=key,
        )
        await storage.commit()
    logger.info('已从留档视频抽出音频（article_pk=%s）', article_pk)


__all__ = ['EXPORT_BITRATE', 'audio_state', 'ensure_audio']
