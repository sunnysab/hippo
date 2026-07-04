"""Shared sync data structures and observer hooks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .models import AccountCredential


class SyncMode(StrEnum):
    full = 'full'
    incremental = 'incremental'
    recent = 'recent'
    range = 'range'

    def __str__(self) -> str:  # pragma: no cover - click displays value
        return self.value


@dataclass(frozen=True)
class SyncConfig:
    mode: SyncMode | None
    page_size: int
    sleep_seconds: float
    reset: bool
    recent_days: int | None
    since_date: str | None
    until_date: str | None
    force: bool
    skip_minutes: int | None
    download_content: bool
    download_images: bool
    content_limit: int | None


@dataclass(frozen=True)
class SyncPlan:
    since_timestamp: int | None
    until_timestamp: int | None
    stop_on_existing: bool
    full_synced_hint: bool
    resume_key: str | None
    complete_key: str | None


@dataclass(frozen=True)
class SyncSummary:
    total_saved: int
    page_count: int
    completed: bool


@dataclass(frozen=True)
class SyncAccountResult:
    biz: str
    nickname: str
    saved: int
    completed: bool
    skipped: bool
    skip_reason: str | None
    failed: bool = False
    error: str | None = None


@dataclass(frozen=True)
class SyncReport:
    total_saved: int
    summary: list[tuple[str, int]]
    details: list[SyncAccountResult]
    downloaded: int = 0
    failed_accounts: int = 0
    accounts_total: int = 0
    accounts_done: int = 0
    current_account: dict[str, Any] | None = None


@dataclass
class AccountProgress:
    biz: str
    nickname: str
    status: str = 'pending'
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
        from .utils import utc_now_iso
        self.updated_at = utc_now_iso()


@dataclass
class SyncTaskState:
    task_id: str
    status: str = 'pending'
    created_at: str = ''
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    group_id: int | None = None
    biz_list: tuple[str, ...] | None = None
    trigger_type: str = 'manual'
    phase: str | None = None
    accounts_total: int = 0
    accounts_done: int = 0
    current_account: dict[str, Any] | None = None
    current_article: dict[str, Any] | None = None
    last_log: str | None = None
    report: dict[str, Any] | None = None
    accounts: dict[str, AccountProgress] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d['accounts'] = [p.__dict__.copy() for p in self.accounts.values()]
        return d

    def to_summary_dict(self) -> dict[str, Any]:
        d = self.to_dict()
        d.pop('accounts', None)
        d.pop('biz_list', None)
        d.pop('trigger_type', None)
        d.pop('report', None)
        return d


class SyncObserver(Protocol):
    def on_log(self, message: str) -> None: ...

    def on_progress(self, *, current: int | None, total: int | None, delta: int | None) -> None: ...

    def on_page(self, payload: dict[str, Any]) -> None: ...

    def on_complete(self, summary: SyncSummary) -> None: ...

    def on_skip(self, reason: str) -> None: ...


class NullSyncObserver(SyncObserver):
    def on_log(self, message: str) -> None:
        return None

    def on_progress(self, *, current: int | None, total: int | None, delta: int | None) -> None:
        return None

    def on_page(self, payload: dict[str, Any]) -> None:
        return None

    def on_complete(self, summary: SyncSummary) -> None:
        return None

    def on_skip(self, reason: str) -> None:
        return None


class SyncJobObserver(Protocol):
    def on_lock_acquired(self) -> None: ...

    def on_accounts_loaded(self, accounts: list[AccountCredential]) -> None: ...

    def on_account_start(self, account: AccountCredential) -> None: ...

    def on_account_stage(self, account: AccountCredential, stage: str) -> None: ...

    def on_account_done(self, result: SyncAccountResult, summary: SyncSummary | None) -> None: ...

    def on_images_start(self) -> None: ...

    def on_images_done(self) -> None: ...


class NullSyncJobObserver:
    def on_lock_acquired(self) -> None:
        return None

    def on_accounts_loaded(self, accounts: list[AccountCredential]) -> None:
        return None

    def on_account_start(self, account: AccountCredential) -> None:
        return None

    def on_account_stage(self, account: AccountCredential, stage: str) -> None:
        return None

    def on_account_done(self, result: SyncAccountResult, summary: SyncSummary | None) -> None:
        return None

    def on_images_start(self) -> None:
        return None

    def on_images_done(self) -> None:
        return None


__all__ = [
    'AccountProgress',
    'NullSyncJobObserver',
    'NullSyncObserver',
    'SyncAccountResult',
    'SyncConfig',
    'SyncJobObserver',
    'SyncMode',
    'SyncObserver',
    'SyncPlan',
    'SyncReport',
    'SyncSummary',
    'SyncTaskState',
]
