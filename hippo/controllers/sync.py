"""Sync controller for account article synchronization."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable

import typer
from tqdm import tqdm

from ..http import MPClient
from ..models import AccountCredential
from ..storage import PostgresStorage, open_storage
from ..sync_core import SyncInterrupted
from ..sync_service import ArticleSyncService, SyncRunError, append_sync_history
from ..sync_types import SyncConfig, SyncMode, SyncObserver, SyncReport
from ..utils import format_table


class TqdmSyncObserver(NullSyncObserver):
    def __init__(self, progress: tqdm | None, account: AccountCredential) -> None:
        self._progress = progress
        self._account = account

    def on_log(self, message: str) -> None:
        _pbar_write(self._progress, message)

    def on_progress(self, *, current: int | None, total: int | None, delta: int | None) -> None:
        if self._progress is None:
            return
        if total and total > 0 and self._progress.total != total:
            self._progress.total = total
        if delta:
            self._progress.update(delta)
        elif current is not None:
            self._progress.n = current
            self._progress.refresh()

    def on_skip(self, reason: str) -> None:
        if not self._progress:
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_cli_sync_history(
    storage: PostgresStorage,
    *,
    started_at: str,
    finished_at: str,
    status: str,
    saved: int,
    error: str = '',
) -> None:
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


def _build_sync_config(
    *,
    mode: SyncMode,
    page_size: int,
    page_limit: int | None,
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
        page_limit=page_limit,
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
    account_map = {account.biz: account for account in accounts}

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
    async with MPClient() as client:
        service = ArticleSyncService(
            storage=storage,
            client=client,
            login_flow=login_flow,
            on_login_required=_handle_login_expired,
        )
        try:
            report = await service.sync_accounts(
                accounts=accounts,
                config=config,
                bulk=bulk,
                use_resume=bulk,
                observer_factory=observer_factory,
            )
        finally:
            if report:
                for detail in report.details:
                    progress = progress_map.get(detail.biz)
                    if not progress:
                        continue
                    if detail.skipped:
                        account = account_map.get(detail.biz)
                        if account:
                            progress.set_postfix_str(_format_skip_reason(detail.skip_reason or '', account), refresh=True)
                    else:
                        progress.set_postfix_str(_status_label(detail.saved, detail.completed), refresh=True)
                    progress.close()
            else:
                for progress in progress_map.values():
                    progress.set_postfix_str('失败', refresh=True)
                    progress.close()

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
        page_limit=pages,
        sleep_seconds=0,
        reset=False,
        recent_days=recent_days,
        since_date=since_date,
        until_date=until_date,
        force=force,
        skip_minutes=skip_time,
    )
    _validate_cli_config(config)
    started_at = _utc_now_iso()
    with open_storage() as storage:
        account = storage.accounts.get_account(biz)
        typer.echo(f'开始同步 {account.nickname} 的文章')
        try:
            report = await perform_sync(
                storage=storage,
                accounts=[account],
                config=config,
                bulk=False,
                login_flow=login_flow,
            )
            finished_at = _utc_now_iso()
            _append_cli_sync_history(
                storage,
                started_at=started_at,
                finished_at=finished_at,
                status='success',
                saved=report.total_saved,
            )
        except SyncRunError as exc:
            finished_at = _utc_now_iso()
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
            finished_at = _utc_now_iso()
            _append_cli_sync_history(
                storage,
                started_at=started_at,
                finished_at=finished_at,
                status='failed',
                saved=0,
                error='Interrupted',
            )
            raise typer.Exit(code=130)
    if report.summary:
        typer.echo(f'同步完成，共写入 {report.total_saved} 条记录')


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
        page_limit=None,
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

        started_at = _utc_now_iso()
        try:
            report = await perform_sync(
                storage=storage,
                accounts=accounts,
                config=config,
                bulk=True,
                login_flow=login_flow,
            )
            finished_at = _utc_now_iso()
            _append_cli_sync_history(
                storage,
                started_at=started_at,
                finished_at=finished_at,
                status='success',
                saved=report.total_saved,
            )
        except SyncRunError as exc:
            finished_at = _utc_now_iso()
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
            finished_at = _utc_now_iso()
            _append_cli_sync_history(
                storage,
                started_at=started_at,
                finished_at=finished_at,
                status='failed',
                saved=0,
                error='Interrupted',
            )
            raise typer.Exit(code=130)

    if report.summary:
        headers = ['账号', '新增/更新']
        rows = [[name, str(saved)] for name, saved in report.summary]
        table_text = format_table(headers, rows)
        if table_text:
            typer.echo(table_text)
    typer.echo(f'全部账号同步完成，共写入 {report.total_saved} 条记录')


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
        page_limit=None,
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

        started_at = _utc_now_iso()
        try:
            report = await perform_sync(
                storage=storage,
                accounts=accounts,
                config=config,
                bulk=True,
                login_flow=login_flow,
            )
            finished_at = _utc_now_iso()
            _append_cli_sync_history(
                storage,
                started_at=started_at,
                finished_at=finished_at,
                status='success',
                saved=report.total_saved,
            )
        except SyncRunError as exc:
            finished_at = _utc_now_iso()
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
            finished_at = _utc_now_iso()
            _append_cli_sync_history(
                storage,
                started_at=started_at,
                finished_at=finished_at,
                status='failed',
                saved=0,
                error='Interrupted',
            )
            raise typer.Exit(code=130)

    if report.summary:
        headers = ['账号', '新增/更新']
        rows = [[name, str(saved)] for name, saved in report.summary]
        table_text = format_table(headers, rows)
        if table_text:
            typer.echo(table_text)
    typer.echo(f'分组 {group} 同步完成，共写入 {report.total_saved} 条记录')


__all__ = ['SyncMode', 'sync_account_articles', 'sync_all_accounts', 'sync_group_accounts']
