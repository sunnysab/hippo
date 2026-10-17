"""Consuming the media queue.

The sync worker drains this one job at a time: the ASR server serialises inference
anyway, so a wider loop would only queue requests behind each other.

A service that stops answering is treated as an infrastructure failure, not an
article failure. Those jobs go back to pending with their attempt count untouched,
and after a few in a row the round stops — a dead backend must not walk the whole
queue into ``failed``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..logger import get_logger
from ..media_settings import get_media_settings
from ..sources.registry import build_media_sources
from ..storage import PostgresStorage
from .asr import AsrError
from .audio import AudioError
from .pipeline import (
    MediaJobFailure,
    MediaJobSkipped,
    MediaResolver,
    transcribe_job,
)

logger = get_logger(__name__)

#: Consecutive infrastructure failures that stop the round.
MAX_CONSECUTIVE_INFRA_FAILURES = 3


@dataclass(slots=True)
class MediaBatchResult:
    done: int = 0
    failed: int = 0
    skipped: int = 0
    requeued: int = 0
    #: Set when the round stopped because the service was not answering.
    unavailable: str | None = None

    @property
    def handled(self) -> int:
        return self.done + self.failed + self.skipped


async def drain_media_once(
    *,
    storage: PostgresStorage,
    settings: dict[str, Any],
    resolver: MediaResolver,
    limit: int = 1,
) -> MediaBatchResult:
    """Run up to ``limit`` jobs. Everything it touches is committed before it returns."""
    result = MediaBatchResult()
    enabled = bool(settings.get('enabled'))
    if not enabled:
        return result

    requeued = await storage.media_jobs.requeue_stale()
    await storage.commit()
    if requeued:
        logger.warning('重置 %d 条卡在 processing 的转写任务', requeued)

    max_attempts = int(settings.get('transcribe_max_attempts') or 3)
    consecutive = 0
    while result.handled < max(int(limit), 1):
        batch = await storage.media_jobs.take_pending(1)
        await storage.commit()  # 先落「已领取」，崩溃后由 requeue_stale 兜底
        if not batch:
            break
        job = batch[0]
        job_id = int(job['id'])
        try:
            # No client is built here: the pipeline creates one only when a job really
            # reaches the ASR, so an unconfigured endpoint no longer blocks a subtitle
            # or a video archive that needs no transcription at all.
            outcome = await transcribe_job(
                storage=storage,
                job=job,
                settings=settings,
                resolver=resolver,
            )
            await storage.media_jobs.mark_done(job_id)
            result.done += 1
            consecutive = 0
            logger.info(
                '转写完成 article_pk=%s 段数=%s 留档=%s',
                outcome.get('article_pk'),
                outcome.get('segments'),
                outcome.get('archived'),
            )
        except MediaJobSkipped as exc:
            await storage.media_jobs.mark_skipped(job_id, reason=str(exc))
            result.skipped += 1
            consecutive = 0
            logger.info('跳过转写 article_pk=%s：%s', job['article_pk'], exc)
        except AsrError as exc:
            # 服务端的问题：不计 attempts，回 pending 等下一轮（或等运维重启服务）。
            await storage.media_jobs.requeue([job_id], error=str(exc))
            result.requeued += 1
            consecutive += 1
            logger.warning('转写服务不可用：%s', exc)
            if consecutive >= MAX_CONSECUTIVE_INFRA_FAILURES:
                result.unavailable = str(exc)
                break
        except (MediaJobFailure, AudioError) as exc:
            state = await storage.media_jobs.mark_failed(
                job_id,
                error=str(exc),
                retryable=True,
                max_attempts=max_attempts,
            )
            if state == 'failed':
                result.failed += 1
            else:
                result.requeued += 1
            consecutive = 0
            logger.warning('转写失败 article_pk=%s：%s', job['article_pk'], exc)
        except Exception as exc:
            # 未预期的异常按单条文章失败处理，不能让一个坏条目把 worker 拖死。
            await storage.media_jobs.mark_failed(
                job_id,
                error=str(exc),
                retryable=True,
                max_attempts=max_attempts,
            )
            result.failed += 1
            consecutive = 0
            logger.exception('转写出现未预期异常 article_pk=%s', job['article_pk'])
        finally:
            await storage.commit()
    return result


async def run_media_once(*, storage: PostgresStorage, limit: int = 1) -> MediaBatchResult:
    """One round with the settings and resolver the worker would use.

    The sources are rebuilt every round so a changed cookie or endpoint takes
    effect without restarting the worker.
    """
    settings = await get_media_settings(storage)
    if not settings.get('enabled'):
        return MediaBatchResult()
    sources = await build_media_sources(storage=storage, settings=settings)
    try:
        return await drain_media_once(
            storage=storage,
            settings=settings,
            resolver=sources.resolver_registry(),
            limit=limit,
        )
    finally:
        await sources.aclose()


__all__ = ['MAX_CONSECUTIVE_INFRA_FAILURES', 'MediaBatchResult', 'drain_media_once', 'run_media_once']
