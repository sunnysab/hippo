"""Async sync task tracking for API progress polling."""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .models import AccountCredential, ArticleRecord
from .sync_service import SYNC_RUN_LOCK, SyncJobResult, run_sync_job
from .sync_types import SyncAccountResult, SyncObserver, SyncSummary
from .utils import utc_now_iso


def _article_snapshot(record: ArticleRecord | None) -> dict[str, Any] | None:
    if not record:
        return None
    return {
        "article_id": record.article_id,
        "title": record.title,
        "item_show_type": record.item_show_type,
        "link": record.link,
        "publish_at": record.publish_at,
    }


@dataclass
class AccountProgress:
    biz: str
    nickname: str
    status: str = "pending"
    phase: str | None = None
    saved: int = 0
    page_count: int = 0
    article_current: int | None = None
    article_total: int | None = None
    last_article: dict[str, Any] | None = None
    skip_reason: str | None = None
    error: str | None = None
    updated_at: str | None = None

    def touch(self) -> None:
        self.updated_at = utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "biz": self.biz,
            "nickname": self.nickname,
            "status": self.status,
            "phase": self.phase,
            "saved": self.saved,
            "page_count": self.page_count,
            "article_current": self.article_current,
            "article_total": self.article_total,
            "last_article": self.last_article,
            "skip_reason": self.skip_reason,
            "error": self.error,
            "updated_at": self.updated_at,
        }


@dataclass
class SyncTaskState:
    task_id: str
    status: str = "pending"
    created_at: str = field(default_factory=utc_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    group_id: int | None = None
    biz_list: tuple[str, ...] | None = None
    phase: str | None = None
    accounts_total: int = 0
    accounts_done: int = 0
    current_account: dict[str, Any] | None = None
    current_article: dict[str, Any] | None = None
    last_log: str | None = None
    report: dict[str, Any] | None = None
    accounts: dict[str, AccountProgress] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "group_id": self.group_id,
            "phase": self.phase,
            "accounts_total": self.accounts_total,
            "accounts_done": self.accounts_done,
            "current_account": self.current_account,
            "current_article": self.current_article,
            "last_log": self.last_log,
            "report": self.report,
            "accounts": [progress.to_dict() for progress in self.accounts.values()],
        }

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            'task_id': self.task_id,
            'status': self.status,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'error': self.error,
            'group_id': self.group_id,
            'phase': self.phase,
            'accounts_total': self.accounts_total,
            'accounts_done': self.accounts_done,
            'current_account': self.current_account,
            'current_article': self.current_article,
            'last_log': self.last_log,
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
        records = payload.get("records") or []
        last_record = records[0] if records else None
        with self._lock:
            progress = self._progress()
            progress.page_count = int(payload.get("page_count") or progress.page_count)
            progress.saved += int(payload.get("saved") or 0)
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
            progress.status = "skipped"
            progress.phase = None
            progress.skip_reason = reason
            progress.error = None
            progress.touch()


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
            state.status = "pending"
            state.started_at = None
            state.last_log = "waiting_for_slot"
            state.phase = None

        def on_lock_acquired() -> None:
            with self._lock:
                state.status = "running"
                if not state.started_at:
                    state.started_at = utc_now_iso()
                state.last_log = None

        def on_accounts_loaded(accounts: list[AccountCredential]) -> None:
            with self._lock:
                state.accounts_total = len(accounts)
                for account in accounts:
                    state.accounts.setdefault(
                        account.biz,
                        AccountProgress(biz=account.biz, nickname=account.nickname or account.biz),
                    )

        def on_account_start(account: AccountCredential) -> None:
            with self._lock:
                state.current_account = {
                    "biz": account.biz,
                    "nickname": account.nickname or account.biz,
                }
                state.phase = 'listing'
                state.current_article = None
                progress = state.accounts.setdefault(
                    account.biz,
                    AccountProgress(biz=account.biz, nickname=account.nickname or account.biz),
                )
                progress.status = "running"
                progress.phase = 'listing'
                progress.error = None
                progress.touch()

        def on_account_stage(account: AccountCredential, stage: str) -> None:
            with self._lock:
                if state.current_account and state.current_account.get('biz') != account.biz:
                    return
                state.phase = stage
                progress = state.accounts.get(account.biz)
                if progress and progress.status == 'running':
                    progress.phase = stage
                    progress.touch()

        def on_account_done(result: SyncAccountResult, summary: SyncSummary | None) -> None:
            with self._lock:
                progress = state.accounts.setdefault(
                    result.biz,
                    AccountProgress(biz=result.biz, nickname=result.nickname or result.biz),
                )
                if result.skipped:
                    progress.status = "skipped"
                    progress.skip_reason = result.skip_reason
                    progress.error = None
                elif result.failed:
                    progress.status = 'failed'
                    progress.skip_reason = None
                    progress.error = result.error
                else:
                    progress.status = "completed" if result.completed else "stopped"
                    progress.skip_reason = None
                    progress.error = None
                progress.phase = None
                progress.saved = result.saved
                if summary:
                    progress.page_count = summary.page_count
                progress.touch()
                state.accounts_done += 1
                if state.current_account and state.current_account.get('biz') == result.biz:
                    state.phase = None

        def observer_factory(account: AccountCredential, _: bool) -> SyncObserver:
            return SyncTaskObserver(state=state, account=account, lock=self._lock)

        try:
            result: SyncJobResult = await run_sync_job(
                group_id=state.group_id,
                biz_list=list(state.biz_list) if state.biz_list else None,
                observer_factory=observer_factory,
                on_account_start=on_account_start,
                on_account_done=on_account_done,
                on_accounts_loaded=on_accounts_loaded,
                on_account_stage=on_account_stage,
                lock=SYNC_RUN_LOCK,
                on_lock_acquired=on_lock_acquired,
                on_images_start=lambda: self._set_task_phase(state, 'images', "downloading_images"),
                on_images_done=lambda: self._set_task_phase(state, None, None),
            )
            with self._lock:
                state.report = {
                    "total_saved": result.report.total_saved,
                    "downloaded": result.report.downloaded,
                    "summary": result.report.summary,
                    "failed_accounts": result.report.failed_accounts,
                }
                status = str(result.status.get("status") or "success")
                state.status = status
                state.error = result.error
                state.finished_at = utc_now_iso()
                state.current_account = None
                state.phase = None
        except Exception as exc:
            with self._lock:
                state.status = "failed"
                state.error = str(exc)
                state.finished_at = utc_now_iso()
                state.current_account = None
                state.phase = None

    def _set_task_phase(self, state: SyncTaskState, phase: str | None, log: str | None) -> None:
        with self._lock:
            state.phase = phase
            state.last_log = log


__all__ = ["SyncTaskManager"]
