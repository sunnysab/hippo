"""Shared sync data structures and observer hooks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .models import AccountCredential


@dataclass(frozen=True)
class SyncConfig:
    """一轮同步的运行参数。

    ``mode`` / ``recent_days`` / ``since`` / ``until`` 这些字段随微信读书数据源一起删掉了：
    weixin-rs 的列表接口只按页取，没有时间过滤，留字段只会让 CLI/UI 看起来能选。
    """

    sleep_seconds: float
    force: bool
    skip_minutes: int | None
    max_pages: int | None = None


@dataclass(frozen=True)
class SyncSummary:
    total_saved: int
    page_count: int
    completed: bool


@dataclass(frozen=True)
class SyncAccountResult:
    biz: str
    nickname: str
    #: 结果自带来源：观察者收到它时可能没有账号对象（worker 的进度没有加载阶段），
    #: 靠它才能把结果归到正确的来源分块里。
    source: str = ''
    saved: int = 0
    completed: bool = False
    skipped: bool = False
    skip_reason: str | None = None
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AccountProgress:
    biz: str
    nickname: str
    #: 该账号属于哪个来源。同步页面按它分块，所以它必须跟着进度走，
    #: 而不是让界面拿 biz 去反查（媒体源才有 `source:` 前缀，反查不可靠）。
    source: str = ''
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
    #: 这次任务限定在哪个来源；空值表示全站同步。
    source: str | None = None
    trigger_type: str = 'manual'
    #: 谁发起了这次任务。调度/回填等系统任务为空；普通用户只能看到与自己 id 相同的任务。
    requester_user_id: int | None = None
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

    def on_log(self, message: str) -> None: ...


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

    def on_log(self, message: str) -> None:
        return None


__all__ = [
    'AccountProgress',
    'NullSyncJobObserver',
    'NullSyncObserver',
    'SyncAccountResult',
    'SyncConfig',
    'SyncJobObserver',
    'SyncObserver',
    'SyncReport',
    'SyncSummary',
    'SyncTaskState',
]
