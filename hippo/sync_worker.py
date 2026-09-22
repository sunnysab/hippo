"""Dedicated sync worker that schedules and executes queued sync jobs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from datetime import UTC, datetime
from typing import Any

from .container import build_sync_container
from .models import AccountCredential
from .storage import PostgresStorage, open_storage
from .sync_core import request_sync_cancel
from .sync_service import (
    SyncJobResult,
    run_sync_job,
)
from .sync_settings import (
    SYNC_STARTED_KEY,
    _get_window_hours,
    _is_within_sync_window,
    get_sync_settings,
)
from .sync_tasks import _article_snapshot
from .sync_types import AccountProgress, SyncAccountResult, SyncObserver, SyncSummary
from .weixin_watch import watch_article_push

logger = logging.getLogger(__name__)

# 正文 drain：每轮抓一批（<=daemon 的 [articles].batch_size），节流由 daemon 侧闸门决定
BODY_BATCH = 5
DRAIN_POLL_SECONDS = 60.0

# 图片回填：独立于正文的队列（article_images 里 s3_key 为空的行）与节奏。
# 图片走普通 HTTPS CDN，和正文的客户端协议不是同一个风控池，但同样不能无节制抓。
IMAGE_BATCH = 30
IMAGE_WORKERS = 2
IMAGE_POLL_SECONDS = 120.0


class _WorkerProgressTracker:
    def __init__(self, *, storage: PostgresStorage, task_id: str) -> None:
        self._storage = storage
        self._task_id = task_id
        self._phase: str | None = None
        self._accounts_total = 0
        self._accounts_done = 0
        self._current_account: dict[str, Any] | None = None
        self._current_article: dict[str, Any] | None = None
        self._last_log: str | None = None
        self._accounts: dict[str, AccountProgress] = {}
        self._report: dict[str, Any] | None = None

    def _save(self) -> None:
        with self._storage.transaction():
            self._storage.sync_jobs.update_progress(
                self._task_id,
                phase=self._phase,
                accounts_total=self._accounts_total,
                accounts_done=self._accounts_done,
                current_account=self._current_account,
                current_article=self._current_article,
                last_log=self._last_log,
                accounts=[p.__dict__.copy() for p in self._accounts.values()],
                report=self._report,
            )

    def _progress_for(self, account: AccountCredential) -> AccountProgress:
        progress = self._accounts.get(account.biz)
        if progress is None:
            progress = AccountProgress(
                biz=account.biz,
                nickname=account.nickname or account.biz,
            )
            self._accounts[account.biz] = progress
        return progress

    def on_lock_acquired(self) -> None:
        self._last_log = None
        self._save()

    def on_accounts_loaded(self, accounts: list[AccountCredential]) -> None:
        self._accounts_total = len(accounts)
        for account in accounts:
            self._progress_for(account)
        self._save()

    def on_account_start(self, account: AccountCredential) -> None:
        self._current_account = {
            'biz': account.biz,
            'nickname': account.nickname or account.biz,
        }
        self._current_article = None
        self._phase = 'listing'
        progress = self._progress_for(account)
        progress.status = 'running'
        progress.phase = 'listing'
        progress.error = None
        progress.touch()
        self._save()

    def on_account_stage(self, account: AccountCredential, stage: str) -> None:
        self._phase = stage
        progress = self._progress_for(account)
        if progress.status == 'running':
            progress.phase = stage
            progress.touch()
        self._save()

    def on_account_done(self, result: SyncAccountResult, summary: SyncSummary | None) -> None:
        progress = self._progress_for(AccountCredential(biz=result.biz, nickname=result.nickname or result.biz))
        if result.skipped:
            progress.status = 'skipped'
            progress.skip_reason = result.skip_reason
            progress.error = None
        elif result.failed:
            progress.status = 'failed'
            progress.skip_reason = None
            progress.error = result.error
        else:
            progress.status = 'completed' if result.completed else 'stopped'
            progress.skip_reason = None
            progress.error = None
        progress.phase = None
        progress.saved = result.saved
        if summary:
            progress.page_count = summary.page_count
        progress.touch()
        self._accounts_done += 1
        if self._current_account and self._current_account.get('biz') == result.biz:
            self._current_account = None
            self._phase = None
        self._save()

    def on_images_start(self) -> None:
        self._phase = 'images'
        self._last_log = 'downloading_images'
        self._save()

    def on_images_done(self) -> None:
        self._phase = None
        self._last_log = None
        self._save()

    def on_log(self, message: str) -> None:
        self._last_log = message
        self._save()

    def set_report(self, result: SyncJobResult) -> None:
        self._report = result.report.to_dict()
        self._save()


class _WorkerObserver(SyncObserver):
    def __init__(
        self,
        *,
        tracker: _WorkerProgressTracker,
        account: AccountCredential,
    ) -> None:
        self._tracker = tracker
        self._account = account

    def on_log(self, message: str) -> None:
        self._tracker._last_log = message
        self._tracker._save()

    def on_progress(self, *, current: int | None, total: int | None, delta: int | None) -> None:
        progress = self._tracker._progress_for(self._account)
        progress.article_current = current
        progress.article_total = total
        progress.touch()
        self._tracker._save()

    def on_page(self, payload: dict[str, Any]) -> None:
        records = payload.get('records') or []
        last_record = records[0] if records else None
        progress = self._tracker._progress_for(self._account)
        progress.page_count = int(payload.get('page_count') or progress.page_count)
        progress.saved += int(payload.get('saved') or 0)
        progress.last_article = _article_snapshot(last_record)
        progress.touch()
        self._tracker._current_article = progress.last_article
        self._tracker._save()

    def on_complete(self, summary: SyncSummary) -> None:
        progress = self._tracker._progress_for(self._account)
        progress.page_count = summary.page_count
        progress.saved = summary.total_saved
        progress.touch()
        self._tracker._save()

    def on_skip(self, reason: str) -> None:
        progress = self._tracker._progress_for(self._account)
        progress.status = 'skipped'
        progress.phase = None
        progress.skip_reason = reason
        progress.touch()
        self._tracker._save()


def _parse_meta_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def recover_stale_running_jobs(
    storage: PostgresStorage,
    *,
    stale_after_minutes: int = 15,
) -> int:
    return storage.sync_jobs.recover_stale_running_jobs(
        stale_after_minutes=stale_after_minutes,
    )


def maybe_enqueue_scheduled_job(storage: PostgresStorage) -> bool:
    settings = get_sync_settings(storage)
    if not settings.get('enabled'):
        return False
    start_hour, end_hour = _get_window_hours(settings)
    now = datetime.now()
    if not _is_within_sync_window(now, start_hour=start_hour, end_hour=end_hour):
        return False
    if storage.sync_jobs.has_active_job():
        return False
    interval_seconds = max(int(settings.get('interval_minutes') or 1), 1) * 60
    last_started = _parse_meta_datetime(storage.meta.get(SYNC_STARTED_KEY))
    if last_started is not None:
        elapsed = (datetime.now(UTC) - last_started).total_seconds()
        if elapsed < interval_seconds:
            return False
    with storage.transaction():
        storage.sync_jobs.create_job(trigger_type='scheduled')
    return True


async def _poll_cancel(task_id: str, poll_interval: float = 1.0) -> None:
    while True:
        try:
            with open_storage() as storage:
                if storage.sync_jobs.is_cancelling(task_id):
                    request_sync_cancel()
                    return
        except Exception as exc:
            logger.debug('Cancel poll failed: %s', exc)
        await asyncio.sleep(poll_interval)


async def run_worker_once(*, storage: PostgresStorage, worker_id: str) -> bool:
    job = storage.sync_jobs.claim_next_job(worker_id=worker_id)
    if not job:
        return False
    with storage.transaction():
        storage.sync_jobs.mark_running(job.task_id, worker_id=worker_id)
    tracker = _WorkerProgressTracker(storage=storage, task_id=job.task_id)
    poll_task = asyncio.create_task(_poll_cancel(job.task_id))
    try:
        try:
            result = await run_sync_job(
                group_id=job.group_id,
                biz_list=list(job.biz_list) if job.biz_list else None,
                observer_factory=lambda account, _: _WorkerObserver(tracker=tracker, account=account),
                observer=tracker,
            )
        except Exception as exc:
            with storage.transaction():
                storage.sync_jobs.mark_finished(
                    job.task_id,
                    status='failed',
                    error=str(exc),
                    result=None,
                )
            return True
        tracker.set_report(result)
        with open_storage() as check_storage:
            was_cancelled = check_storage.sync_jobs.is_cancelling(job.task_id)
        final_status = 'cancelled' if was_cancelled else str(result.status.get('status') or 'success')
        final_error = 'Cancelled by user' if was_cancelled else result.error
        with storage.transaction():
            storage.sync_jobs.mark_finished(
                job.task_id,
                status=final_status,
                error=final_error,
                result=result.report.to_dict(),
            )
    finally:
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll_task
    return True


async def drain_bodies_once(storage: PostgresStorage, *, limit: int = BODY_BATCH) -> int:
    """抓一批待处理正文，返回本次落库篇数。

    列表只负责把文章放进 ``article_queue``；这里才是正文真正的入口，是常驻的
    （24h 不断），速率由 daemon 的 ``[articles]`` 预算保证。
    """
    settings = get_sync_settings(storage)
    if not settings.get('download_content'):
        return 0
    if not storage.article_queue.stats().get('pending'):
        return 0
    container = build_sync_container(
        storage=storage,
        enable_download=False,
        enable_images=bool(settings.get('download_images')),
    )
    async with container as app:
        result = await app.weixin_sync.drain(limit=limit)
        if settings.get('download_images') and app.downloader:
            with contextlib.suppress(Exception):
                await app.downloader.wait_for_images()
    if result.ingested or result.failed:
        logger.info('正文 drain：落库 %d 篇，失败 %d 篇', result.ingested, result.failed)
    return result.ingested


async def backfill_images_once(*, batch: int = IMAGE_BATCH, workers: int = IMAGE_WORKERS) -> int:
    """抓一批待下载图片，返回本次成功入库的张数。

    正文只负责把图片登记进 ``article_images``；真正下载/上传 S3 在这里，
    按自己的批量与并发跑（落库语义就是队列：``s3_key IS NULL`` = 待下载）。
    """
    from .image_backfill import backfill_article_images

    try:
        result = await backfill_article_images(limit=batch, workers=workers)
    except RuntimeError as exc:
        if 'image store' in str(exc):
            logger.debug('未配置对象存储，跳过图片回填：%s', exc)
            return 0
        raise
    updated = int(result.get('updated') or 0)
    failed = int(result.get('failed') or 0)
    if updated or failed:
        logger.info('图片回填：入库 %d 张，失败 %d 张', updated, failed)
    return updated


async def _image_backfill_loop(
    batch: int = IMAGE_BATCH,
    poll_interval: float = IMAGE_POLL_SECONDS,
) -> None:
    """常驻图片队列消费：与正文抓取并行，互不干扰。"""
    while True:
        try:
            await backfill_images_once(batch=batch)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('图片回填失败')
        await asyncio.sleep(max(float(poll_interval), 5.0))


async def _body_drain_loop(poll_interval: float = DRAIN_POLL_SECONDS) -> None:
    """常驻正文抓取循环：与列表 job 并行，互不阻塞。"""
    while True:
        try:
            with open_storage() as storage:
                await drain_bodies_once(storage)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('正文 drain 失败')
        await asyncio.sleep(max(float(poll_interval), 1.0))

async def run_sync_worker(
    *,
    poll_interval: float = 5.0,
    worker_id: str | None = None,
    drain_interval: float = DRAIN_POLL_SECONDS,
) -> None:
    resolved_worker_id = worker_id or f'{socket.gethostname()}-{os.getpid()}'
    # 正文抓取跑在独立 task：它经常被 daemon 的节奏闸门挡住几十秒，
    # 不能拖住主循环里「入队新文章 / 执行同步 job」的响应。
    drain_task = asyncio.create_task(_body_drain_loop(drain_interval))
    image_task = asyncio.create_task(_image_backfill_loop())
    # 实时通道：daemon 推 article_push 时立刻入队（列表轮询仍然是兜底）
    watch_task = asyncio.create_task(watch_article_push())
    try:
        while True:
            with open_storage() as storage:
                with storage.transaction():
                    recover_stale_running_jobs(storage)
                    maybe_enqueue_scheduled_job(storage)
                handled = await run_worker_once(storage=storage, worker_id=resolved_worker_id)
            if handled:
                continue
            await asyncio.sleep(max(float(poll_interval), 0.2))
    finally:
        for task in (drain_task, image_task, watch_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


__all__ = [
    'backfill_images_once',
    'drain_bodies_once',
    'maybe_enqueue_scheduled_job',
    'recover_stale_running_jobs',
    'run_sync_worker',
    'run_worker_once',
]
