"""Sync controller for account article synchronization."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

import typer
from tqdm import tqdm

from ..container import build_sync_container
from ..exceptions import SyncInterrupted
from ..models import AccountCredential
from ..storage import PostgresStorage, open_storage
from ..sync_service import ArticleSyncService, SyncRunError
from ..sync_settings import append_sync_history, set_sync_state
from ..sync_types import (
    NullSyncJobObserver,
    NullSyncObserver,
    SyncAccountResult,
    SyncConfig,
    SyncMode,
    SyncObserver,
    SyncReport,
    SyncSummary,
)
from ..utils import utc_now_iso


class _CliSyncJobObserver(NullSyncJobObserver):
    def __init__(self, on_done: Callable[[SyncAccountResult, SyncSummary | None], None]) -> None:
        self._on_done = on_done

    def on_account_done(self, result: SyncAccountResult, summary: SyncSummary | None) -> None:
        self._on_done(result, summary)


class TqdmSyncObserver(NullSyncObserver):
    def __init__(self, progress: tqdm | None, account: AccountCredential) -> None:
        self._progress = progress
        self._account = account
        self._saved = 0

    def on_log(self, message: str) -> None:
        _pbar_write(self._progress, message)

    def on_progress(self, *, current: int | None, total: int | None, delta: int | None) -> None:
        return None

    def on_page(self, payload: dict[str, object]) -> None:
        if self._progress is None:
            return
        saved = int(payload.get('saved') or 0)
        if saved <= 0:
            return
        self._saved += saved
        self._progress.total = self._saved
        self._progress.n = self._saved
        self._progress.refresh()

    def on_complete(self, summary: SyncSummary) -> None:
        if self._progress is None:
            return
        self._saved = summary.total_saved
        if self._saved > 0:
            self._progress.total = self._saved
            self._progress.n = self._saved
            self._progress.refresh()

    def on_skip(self, reason: str) -> None:
        if self._progress is None:
            return
        label = _format_skip_reason(reason, self._account)
        self._progress.set_postfix_str(label, refresh=True)


def _enforce_exclusive_flags(force: bool, skip_minutes: int | None) -> None:
    if force and skip_minutes is not None:
        raise typer.BadParameter('--force 与 --skip-time 不能同时使用')


def _format_last_synced(last_synced_at: datetime | None) -> str:
    return last_synced_at.isoformat(timespec='seconds') if last_synced_at else '-'


def _format_skip_reason(reason: str, account: AccountCredential) -> str:
    if reason == 'disabled':
        return '跳过(已禁用)'
    if reason == 'completed_today':
        return '跳过(今日已完成)'
    if reason == 'recently_synced':
        last_synced = _format_last_synced(account.last_synced_at)
        return f'跳过(近期已同步 {last_synced})'
    if reason == 'freq_control':
        return '跳过(频控)'
    if reason == 'sync_interval':
        return '跳过(未到同步周期)'
    return '跳过'


def _parse_sync_date(value: str | None, *, label: str, end_of_day: bool = False) -> int | None:
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f'{label} must be YYYY-MM-DD') from exc
    dt = datetime(parsed.year, parsed.month, parsed.day)
    if end_of_day:
        dt = dt + timedelta(days=1) - timedelta(seconds=1)
    return int(dt.timestamp())


def _validate_cli_config(config: SyncConfig) -> None:
    if config.mode == SyncMode.recent and config.recent_days is None:
        raise typer.BadParameter('--recent-days is required for --mode recent.')
    if config.mode == SyncMode.range:
        if not config.since_date:
            raise typer.BadParameter('--since is required for --mode range.')
        since = _parse_sync_date(config.since_date, label='--since')
        until = _parse_sync_date(config.until_date, label='--until', end_of_day=True)
        if until is not None and since is not None and until < since:
            raise typer.BadParameter('--until must be on or after --since.')


def _status_label(saved: int, completed: bool) -> str:
    if completed and saved == 0:
        return '已是最新'
    if completed:
        return '成功'
    return '未完成'


def _append_cli_sync_history(
    storage: PostgresStorage,
    *,
    started_at: str,
    finished_at: str,
    status: str,
    saved: int,
    error: str = '',
) -> None:
    set_sync_state(
        storage,
        status=status,
        error=error,
        started_at=started_at,
        finished_at=finished_at,
    )
    append_sync_history(
        storage,
        {
            'started_at': started_at,
            'finished_at': finished_at,
            'status': status,
            'error': error,
            'saved': saved,
            'source': 'cli',
        },
    )


def _pbar_write(progress: tqdm | None, message: str) -> None:
    if progress is not None:
        progress.write(message)
    else:
        typer.echo(message)


def _handle_login_expired() -> bool:
    typer.echo('登录状态可能已失效，请先运行 `hippo login` 后重试同步。')
    return False


@asynccontextmanager
async def _sync_error_handler(
    storage: PostgresStorage,
    *,
    started_at: str,
) -> AsyncGenerator[None]:
    try:
        yield
    except SyncRunError as exc:
        finished_at = utc_now_iso()
        status = 'login_required' if exc.login_required else 'failed'
        _append_cli_sync_history(
            storage,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            saved=0,
            error=str(exc),
        )
        raise typer.Exit(code=1)
    except SyncInterrupted:
        finished_at = utc_now_iso()
        _append_cli_sync_history(
            storage,
            started_at=started_at,
            finished_at=finished_at,
            status='failed',
            saved=0,
            error='Interrupted',
        )
        raise typer.Exit(code=130)


def _build_sync_config(
    *,
    mode: SyncMode,
    page_size: int,
    sleep_seconds: float,
    reset: bool,
    recent_days: int | None,
    since_date: str | None,
    until_date: str | None,
    force: bool,
    skip_minutes: int | None,
) -> SyncConfig:
    return SyncConfig(
        mode=mode,
        page_size=page_size,
        sleep_seconds=sleep_seconds,
        reset=reset,
        recent_days=recent_days,
        since_date=since_date,
        until_date=until_date,
        force=force,
        skip_minutes=skip_minutes,
        download_content=False,
        download_images=False,
        content_limit=0,
    )


async def perform_sync(
    *,
    storage: PostgresStorage,
    accounts: list[AccountCredential],
    config: SyncConfig,
    bulk: bool,
    login_flow: Callable[..., Awaitable[None]] | None,
) -> SyncReport:
    _enforce_exclusive_flags(config.force, config.skip_minutes)
    progress_map: dict[str, tqdm] = {}
    closed_progress_biz: set[str] = set()
    account_map = {account.biz: account for account in accounts}

    def close_account_progress(
        *,
        biz: str,
        detail: SyncAccountResult | None = None,
        failed: bool = False,
    ) -> None:
        if not biz or biz in closed_progress_biz:
            return
        progress = progress_map.get(biz)
        if progress is None:
            return
        if failed:
            progress.set_postfix_str('失败', refresh=True)
            progress.close()
            closed_progress_biz.add(biz)
            return
        if detail is None:
            return
        skipped = detail.skipped
        failed = detail.failed
        skip_reason = detail.skip_reason
        saved = detail.saved
        completed = detail.completed
        if failed:
            progress.set_postfix_str('失败', refresh=True)
        elif skipped:
            account = account_map.get(biz)
            if account:
                progress.set_postfix_str(_format_skip_reason(str(skip_reason or ''), account), refresh=True)
        else:
            progress.set_postfix_str(_status_label(saved, completed), refresh=True)
        progress.close()
        closed_progress_biz.add(biz)

    def observer_factory(account: AccountCredential, is_bulk: bool) -> SyncObserver:
        desc = f'同步 {account.nickname}' if not is_bulk else f'同步 {account.nickname} ({account.biz})'
        progress = tqdm(
            total=None,
            desc=desc,
            unit='msg',
            dynamic_ncols=True,
            leave=True,
        )
        progress_map[account.biz] = progress
        return TqdmSyncObserver(progress, account)

    report: SyncReport | None = None
    container = build_sync_container(storage=storage, enable_download=False, enable_images=False)
    async with container as app:
        service = ArticleSyncService(
            storage=storage,
            client=app.api_client,
            login_flow=login_flow,
            on_login_required=_handle_login_expired,
        )
        try:
            cli_observer = _CliSyncJobObserver(
                on_done=lambda result, _: close_account_progress(biz=result.biz, detail=result),
            )
            report = await service.sync_accounts(
                accounts=accounts,
                config=config,
                bulk=bulk,
                use_resume=bulk,
                observer_factory=observer_factory,
                observer=cli_observer,
            )
        finally:
            if report:
                for detail in report.details:
                    close_account_progress(biz=detail.biz, detail=detail)
            else:
                for biz in progress_map:
                    close_account_progress(biz=biz, failed=True)

    if report is None:
        raise RuntimeError('Sync report missing')
    return report


async def sync_account_articles(
    *,
    biz: str | None,
    pages: int,
    page_size: int,
    mode: SyncMode,
    recent_days: int | None,
    since_date: str | None,
    until_date: str | None,
    force: bool,
    skip_time: int | None,
    login_flow: Callable[..., Awaitable[None]] | None,
) -> None:
    config = _build_sync_config(
        mode=mode,
        page_size=page_size,
        sleep_seconds=0,
        reset=False,
        recent_days=recent_days,
        since_date=since_date,
        until_date=until_date,
        force=force,
        skip_minutes=skip_time,
    )
    _validate_cli_config(config)
    started_at = utc_now_iso()
    with open_storage() as storage:
        account = storage.accounts.get_account(biz)
        typer.echo(f'开始同步 {account.nickname} 的文章')
        async with _sync_error_handler(storage, started_at=started_at):
            report = await perform_sync(
                storage=storage,
                accounts=[account],
                config=config,
                bulk=False,
                login_flow=login_flow,
            )
            finished_at = utc_now_iso()
            _append_cli_sync_history(
                storage,
                started_at=started_at,
                finished_at=finished_at,
                status='success',
                saved=report.total_saved,
            )
    if report.summary:
        typer.echo(f'同步完成，共新增 {report.total_saved} 条记录')


async def sync_all_accounts(
    *,
    page_size: int,
    sleep_seconds: float,
    reset: bool,
    mode: SyncMode,
    recent_days: int | None,
    since_date: str | None,
    until_date: str | None,
    force: bool,
    skip_time: int | None,
    login_flow: Callable[..., Awaitable[None]] | None,
) -> None:
    config = _build_sync_config(
        mode=mode,
        page_size=page_size,
        sleep_seconds=sleep_seconds,
        reset=reset,
        recent_days=recent_days,
        since_date=since_date,
        until_date=until_date,
        force=force,
        skip_minutes=skip_time,
    )
    _validate_cli_config(config)

    with open_storage() as storage:
        accounts = storage.accounts.list_accounts()
        if not accounts:
            typer.echo('尚未保存任何账号，使用 `account add` 添加')
            return

        header = '开始同步全部账号（从最新文章往更早翻页）'
        if reset:
            header = '开始同步全部账号（重置断点，从最新文章往更早翻页）'
        if sleep_seconds > 0:
            header += f' 每页间隔 {sleep_seconds} 秒'
        typer.echo(header)

        started_at = utc_now_iso()
        async with _sync_error_handler(storage, started_at=started_at):
            report = await perform_sync(
                storage=storage,
                accounts=accounts,
                config=config,
                bulk=True,
                login_flow=login_flow,
            )
            finished_at = utc_now_iso()
            _append_cli_sync_history(
                storage,
                started_at=started_at,
                finished_at=finished_at,
                status='success',
                saved=report.total_saved,
            )

    typer.echo(f'全部账号同步完成，共新增 {report.total_saved} 条记录')


async def sync_group_accounts(
    *,
    group: str,
    page_size: int,
    sleep_seconds: float,
    reset: bool,
    mode: SyncMode,
    recent_days: int | None,
    since_date: str | None,
    until_date: str | None,
    force: bool,
    skip_time: int | None,
    login_flow: Callable[..., Awaitable[None]] | None,
) -> None:
    config = _build_sync_config(
        mode=mode,
        page_size=page_size,
        sleep_seconds=sleep_seconds,
        reset=reset,
        recent_days=recent_days,
        since_date=since_date,
        until_date=until_date,
        force=force,
        skip_minutes=skip_time,
    )
    _validate_cli_config(config)

    with open_storage() as storage:
        groups = storage.groups.list_groups()
        target = next((item for item in groups if item.name == group), None)
        if not target:
            typer.echo('分组不存在，请先创建分组')
            return
        accounts = storage.accounts.list_accounts(group=group)
        if not accounts:
            typer.echo('分组内暂无账号')
            return

        header = f'开始同步分组 {group}（从最新文章往更早翻页）'
        if reset:
            header = f'开始同步分组 {group}（重置断点，从最新文章往更早翻页）'
        if sleep_seconds > 0:
            header += f' 每页间隔 {sleep_seconds} 秒'
        typer.echo(header)

        started_at = utc_now_iso()
        async with _sync_error_handler(storage, started_at=started_at):
            report = await perform_sync(
                storage=storage,
                accounts=accounts,
                config=config,
                bulk=True,
                login_flow=login_flow,
            )
            finished_at = utc_now_iso()
            _append_cli_sync_history(
                storage,
                started_at=started_at,
                finished_at=finished_at,
                status='success',
                saved=report.total_saved,
            )

    typer.echo(f'分组 {group} 同步完成，共新增 {report.total_saved} 条记录')


__all__ = ['SyncMode', 'sync_account_articles', 'sync_all_accounts', 'sync_group_accounts']
