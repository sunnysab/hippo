"""Async sync task tracking for API progress polling."""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any

from .models import AccountCredential, ArticleRecord
from .sync_service import SYNC_RUN_LOCK, SyncJobResult, run_sync_job
from .sync_types import (
    AccountProgress,
    SyncAccountResult,
    SyncObserver,
    SyncSummary,
    SyncTaskState,
)
from .utils import utc_now_iso


def _article_snapshot(record: ArticleRecord | None) -> dict[str, Any] | None:
    if not record:
        return None
    return {
        'article_id': record.article_id,
        'title': record.title,
        'item_show_type': record.item_show_type,
        'link': record.link,
        'publish_at': record.publish_at,
    }


class SyncTaskObserver(SyncObserver):
    def __init__(self, *, state: SyncTaskState, account: AccountCredential, lock: threading.Lock) -> None:
        self._state = state
        self._account = account
        self._lock = lock

    def _progress(self) -> AccountProgress:
        progress = self._state.accounts.get(self._account.biz)
        if not progress:
            progress = AccountProgress(biz=self._account.biz, nickname=self._account.nickname or self._account.biz)
            self._state.accounts[self._account.biz] = progress
        return progress

    def on_log(self, message: str) -> None:
        with self._lock:
            self._state.last_log = message

    def on_progress(self, *, current: int | None, total: int | None, delta: int | None) -> None:
        with self._lock:
            progress = self._progress()
            progress.article_current = current
            progress.article_total = total
            progress.touch()

    def on_page(self, payload: dict[str, Any]) -> None:
        records = payload.get('records') or []
        last_record = records[0] if records else None
        with self._lock:
            progress = self._progress()
            progress.page_count = int(payload.get('page_count') or progress.page_count)
            progress.saved += int(payload.get('saved') or 0)
            progress.last_article = _article_snapshot(last_record)
            progress.touch()
            self._state.current_article = progress.last_article

    def on_complete(self, summary: SyncSummary) -> None:
        with self._lock:
            progress = self._progress()
            progress.page_count = summary.page_count
            progress.saved = summary.total_saved
            progress.touch()

    def on_skip(self, reason: str) -> None:
        with self._lock:
            progress = self._progress()
            progress.status = 'skipped'
            progress.phase = None
            progress.skip_reason = reason
            progress.error = None
            progress.touch()


class _SyncTaskJobObserver:
    def __init__(self, manager: SyncTaskManager, state: SyncTaskState) -> None:
        self._manager = manager
        self._state = state

    def on_lock_acquired(self) -> None:
        with self._manager._lock:
            self._state.status = 'running'
            if not self._state.started_at:
                self._state.started_at = utc_now_iso()
            self._state.last_log = None

    def on_accounts_loaded(self, accounts: list[AccountCredential]) -> None:
        with self._manager._lock:
            self._state.accounts_total = len(accounts)
            for account in accounts:
                self._state.accounts.setdefault(
                    account.biz,
                    AccountProgress(biz=account.biz, nickname=account.nickname or account.biz),
                )

    def on_account_start(self, account: AccountCredential) -> None:
        with self._manager._lock:
            self._state.current_account = {
                'biz': account.biz,
                'nickname': account.nickname or account.biz,
            }
            self._state.phase = 'listing'
            self._state.current_article = None
            progress = self._state.accounts.setdefault(
                account.biz,
                AccountProgress(biz=account.biz, nickname=account.nickname or account.biz),
            )
            progress.status = 'running'
            progress.phase = 'listing'
            progress.error = None
            progress.touch()

    def on_account_stage(self, account: AccountCredential, stage: str) -> None:
        with self._manager._lock:
            if self._state.current_account and self._state.current_account.get('biz') != account.biz:
                return
            self._state.phase = stage
            progress = self._state.accounts.get(account.biz)
            if progress and progress.status == 'running':
                progress.phase = stage
                progress.touch()

    def on_account_done(self, result: SyncAccountResult, summary: SyncSummary | None) -> None:
        with self._manager._lock:
            progress = self._state.accounts.setdefault(
                result.biz,
                AccountProgress(biz=result.biz, nickname=result.nickname or result.biz),
            )
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
            self._state.accounts_done += 1
            if self._state.current_account and self._state.current_account.get('biz') == result.biz:
                self._state.phase = None

    def on_images_start(self) -> None:
        self._manager._set_task_phase(self._state, 'images', 'downloading_images')

    def on_images_done(self) -> None:
        self._manager._set_task_phase(self._state, None, None)


class SyncTaskManager:
    def __init__(self, *, max_tasks: int = 50) -> None:
        self._tasks: dict[str, SyncTaskState] = {}
        self._max_tasks = max_tasks
        self._lock = threading.Lock()

    def create_sync_task(
        self,
        *,
        group_id: int | None = None,
        biz_list: list[str] | None = None,
    ) -> str:
        task_id = uuid.uuid4().hex
        state = SyncTaskState(
            task_id=task_id,
            group_id=group_id,
            biz_list=tuple(biz_list) if biz_list else None,
        )
        with self._lock:
            self._tasks[task_id] = state
            self._trim()
        asyncio.create_task(self._run_task(state))
        return task_id

    def get_task(self, task_id: str) -> SyncTaskState | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self) -> list[SyncTaskState]:
        with self._lock:
            return list(self._tasks.values())

    def _trim(self) -> None:
        if len(self._tasks) <= self._max_tasks:
            return
        finished = [item for item in self._tasks.values() if item.finished_at]
        finished.sort(key=lambda item: item.finished_at or item.created_at)
        for item in finished[: max(0, len(self._tasks) - self._max_tasks)]:
            self._tasks.pop(item.task_id, None)

    async def _run_task(self, state: SyncTaskState) -> None:
        with self._lock:
            state.status = 'pending'
            state.started_at = None
            state.last_log = 'waiting_for_slot'
            state.phase = None

        def observer_factory(account: AccountCredential, _: bool) -> SyncObserver:
            return SyncTaskObserver(state=state, account=account, lock=self._lock)

        job_observer = _SyncTaskJobObserver(self, state)
        try:
            result: SyncJobResult = await run_sync_job(
                group_id=state.group_id,
                biz_list=list(state.biz_list) if state.biz_list else None,
                observer_factory=observer_factory,
                observer=job_observer,
                lock=SYNC_RUN_LOCK,
            )
            with self._lock:
                state.report = {
                    'total_saved': result.report.total_saved,
                    'downloaded': result.report.downloaded,
                    'summary': result.report.summary,
                    'failed_accounts': result.report.failed_accounts,
                }
                status = str(result.status.get('status') or 'success')
                state.status = status
                state.error = result.error
                state.finished_at = utc_now_iso()
                state.current_account = None
                state.phase = None
        except Exception as exc:
            with self._lock:
                state.status = 'failed'
                state.error = str(exc)
                state.finished_at = utc_now_iso()
                state.current_account = None
                state.phase = None

    def _set_task_phase(self, state: SyncTaskState, phase: str | None, log: str | None) -> None:
        with self._lock:
            state.phase = phase
            state.last_log = log


__all__ = ['SyncTaskManager']
