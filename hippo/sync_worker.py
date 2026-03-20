"""Dedicated sync worker that schedules and executes queued sync jobs."""

from __future__ import annotations

import asyncio
import os
import socket
from datetime import datetime, timezone
from typing import Any

from .models import AccountCredential
from .storage import PostgresStorage, open_storage
from .sync_jobs import SyncJobState
from .sync_service import (
    SYNC_STARTED_KEY,
    SyncJobResult,
    _get_window_hours,
    _is_within_sync_window,
    get_sync_settings,
    run_sync_job,
)
from .sync_tasks import _article_snapshot
from .sync_types import SyncAccountResult, SyncObserver, SyncSummary


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_report(result: SyncJobResult) -> dict[str, Any]:
    return {
        'total_saved': result.report.total_saved,
        'downloaded': result.report.downloaded,
        'summary': result.report.summary,
    }


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
        self._accounts: dict[str, dict[str, Any]] = {}
        self._report: dict[str, Any] | None = None

    def _save(self) -> None:
        self._storage.sync_jobs.update_progress(
            self._task_id,
            phase=self._phase,
            accounts_total=self._accounts_total,
            accounts_done=self._accounts_done,
            current_account=self._current_account,
            current_article=self._current_article,
            last_log=self._last_log,
            accounts=list(self._accounts.values()),
            report=self._report,
        )

    def _progress_for(self, account: AccountCredential) -> dict[str, Any]:
        progress = self._accounts.get(account.biz)
        if progress is None:
            progress = {
                'biz': account.biz,
                'nickname': account.nickname or account.biz,
                'status': 'pending',
                'phase': None,
                'saved': 0,
                'page_count': 0,
                'article_current': None,
                'article_total': None,
                'last_article': None,
                'skip_reason': None,
                'updated_at': None,
            }
            self._accounts[account.biz] = progress
        return progress

    def _touch(self, progress: dict[str, Any]) -> None:
        progress['updated_at'] = _utc_now_iso()

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
        progress['status'] = 'running'
        progress['phase'] = 'listing'
        self._touch(progress)
        self._save()

    def on_account_stage(self, account: AccountCredential, stage: str) -> None:
        self._phase = stage
        progress = self._progress_for(account)
        if progress['status'] == 'running':
            progress['phase'] = stage
            self._touch(progress)
        self._save()

    def on_account_done(self, result: SyncAccountResult, summary: SyncSummary | None) -> None:
        progress = self._accounts.setdefault(
            result.biz,
            {
                'biz': result.biz,
                'nickname': result.nickname or result.biz,
                'status': 'pending',
                'phase': None,
                'saved': 0,
                'page_count': 0,
                'article_current': None,
                'article_total': None,
                'last_article': None,
                'skip_reason': None,
                'updated_at': None,
            },
        )
        if result.skipped:
            progress['status'] = 'skipped'
            progress['skip_reason'] = result.skip_reason
        else:
            progress['status'] = 'completed' if result.completed else 'stopped'
        progress['phase'] = None
        progress['saved'] = result.saved
        if summary:
            progress['page_count'] = summary.page_count
        self._touch(progress)
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

    def set_report(self, result: SyncJobResult) -> None:
        self._report = _normalize_report(result)
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
        progress['article_current'] = current
        progress['article_total'] = total
        self._tracker._touch(progress)
        self._tracker._save()

    def on_page(self, payload: dict[str, Any]) -> None:
        records = payload.get('records') or []
        last_record = records[0] if records else None
        progress = self._tracker._progress_for(self._account)
        progress['page_count'] = int(payload.get('page_count') or progress['page_count'])
        progress['saved'] += int(payload.get('saved') or 0)
        progress['last_article'] = _article_snapshot(last_record)
        self._tracker._touch(progress)
        self._tracker._current_article = progress['last_article']
        self._tracker._save()

    def on_complete(self, summary: SyncSummary) -> None:
        progress = self._tracker._progress_for(self._account)
        progress['page_count'] = summary.page_count
        progress['saved'] = summary.total_saved
        self._tracker._touch(progress)
        self._tracker._save()

    def on_skip(self, reason: str) -> None:
        progress = self._tracker._progress_for(self._account)
        progress['status'] = 'skipped'
        progress['phase'] = None
        progress['skip_reason'] = reason
        self._tracker._touch(progress)
        self._tracker._save()


def _parse_meta_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
        elapsed = (datetime.now(timezone.utc) - last_started).total_seconds()
        if elapsed < interval_seconds:
            return False
    with storage.transaction():
        storage.sync_jobs.create_job(trigger_type='scheduled')
    return True


async def run_worker_once(*, storage: PostgresStorage, worker_id: str) -> bool:
    job = storage.sync_jobs.claim_next_job(worker_id=worker_id)
    if not job:
        return False
    with storage.transaction():
        storage.sync_jobs.mark_running(job.task_id, worker_id=worker_id)
    tracker = _WorkerProgressTracker(storage=storage, task_id=job.task_id)
    try:
        result = await run_sync_job(
            group_id=job.group_id,
            biz_list=list(job.biz_list) if job.biz_list else None,
            observer_factory=lambda account, _: _WorkerObserver(tracker=tracker, account=account),
            on_account_start=tracker.on_account_start,
            on_account_done=tracker.on_account_done,
            on_accounts_loaded=tracker.on_accounts_loaded,
            on_account_stage=tracker.on_account_stage,
            on_lock_acquired=tracker.on_lock_acquired,
            on_images_start=tracker.on_images_start,
            on_images_done=tracker.on_images_done,
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
    with storage.transaction():
        storage.sync_jobs.mark_finished(
            job.task_id,
            status=str(result.status.get('status') or 'success'),
            error=result.error,
            result=_normalize_report(result),
        )
    return True


async def run_sync_worker(
    *,
    poll_interval: float = 5.0,
    worker_id: str | None = None,
) -> None:
    resolved_worker_id = worker_id or f'{socket.gethostname()}-{os.getpid()}'
    while True:
        with open_storage() as storage:
            with storage.transaction():
                maybe_enqueue_scheduled_job(storage)
            handled = await run_worker_once(storage=storage, worker_id=resolved_worker_id)
        if handled:
            continue
        await asyncio.sleep(max(float(poll_interval), 0.2))


__all__ = ['maybe_enqueue_scheduled_job', 'run_sync_worker', 'run_worker_once']
