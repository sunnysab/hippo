"""Sync controller for account article synchronization.

CLI 入口保持同步执行（跑完再退出），但内部语义和 worker 一致：列表阶段只把文章
放进 ``article_queue``，正文由队列 drain 抓。tqdm 反映的是账号级进度。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace

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
    SyncObserver,
    SyncReport,
    SyncSummary,
)
from ..utils import utc_now_iso

# CLI 跑完一轮就退出，所以正文只顺带抓一批；常驻抓取是 worker 里 drain 循环的活
CLI_BODY_BATCH = 5


class _CliSyncJobObserver(NullSyncJobObserver):
    def __init__(self, on_done: Callable[[SyncAccountResult, SyncSummary | None], None]) -> None:
        self._on_done = on_done

    def on_account_done(self, result: SyncAccountResult, summary: SyncSummary | None) -> None:
        self._on_done(result, summary)


class TqdmSyncObserver(NullSyncObserver):
    def __init__(self, progress: tqdm | None, account: AccountCredential) -> None:
        self._progress = progress
        self._account = account

    def on_log(self, message: str) -> None:
        _pbar_write(self._progress, message)

    def on_skip(self, reason: str) -> None:
        if self._progress is None:
            return
        self._progress.set_postfix_str(_format_skip_reason(reason, self._account), refresh=True)


def _enforce_exclusive_flags(force: bool, skip_minutes: int | None) -> None:
    if force and skip_minutes is not None:
        raise typer.BadParameter('--force 与 --skip-time 不能同时使用')


def _format_skip_reason(reason: str, account: AccountCredential) -> str:
    if reason == 'disabled':
        return '跳过(已禁用)'
    if reason == 'recently_synced':
        last_synced = account.last_synced_at.isoformat(timespec='seconds') if account.last_synced_at else '-'
        return f'跳过(近期已同步 {last_synced})'
    if reason == 'sync_interval':
        return '跳过(未到同步周期)'
    if reason == 'no_source_key':
        return '跳过(没有可用的 gh_/alias)'
    return '跳过'


def _status_label(saved: int, completed: bool) -> str:
    if completed and saved == 0:
        return '已是最新'
    if completed:
        return '成功'
    return '未完成'


def _build_sync_config(
    *,
    sleep_seconds: float,
    force: bool,
    skip_minutes: int | None,
    max_pages: int | None = None,
) -> SyncConfig:
    _enforce_exclusive_flags(force, skip_minutes)
    return SyncConfig(
        sleep_seconds=max(sleep_seconds, 0.0),
        force=force,
        skip_minutes=skip_minutes,
        max_pages=max_pages,
    )


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


@asynccontextmanager
async def _sync_error_handler(storage: PostgresStorage, *, started_at: str):
    try:
        yield
    except SyncRunError as exc:
        _append_cli_sync_history(
            storage,
            started_at=started_at,
            finished_at=utc_now_iso(),
            status='failed',
            saved=0,
            error=str(exc),
        )
        raise typer.Exit(code=1)
    except SyncInterrupted:
        _append_cli_sync_history(
            storage,
            started_at=started_at,
            finished_at=utc_now_iso(),
            status='failed',
            saved=0,
            error='Interrupted',
        )
        raise typer.Exit(code=130)


async def perform_sync(
    *,
    storage: PostgresStorage,
    accounts: list[AccountCredential],
    config: SyncConfig,
    bulk: bool,
    download: bool = True,
) -> SyncReport:
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
        if failed or (detail is not None and detail.failed):
            progress.set_postfix_str('失败', refresh=True)
        elif detail is not None and detail.skipped:
            account = account_map.get(biz)
            if account:
                progress.set_postfix_str(_format_skip_reason(str(detail.skip_reason or ''), account), refresh=True)
        elif detail is not None:
            progress.set_postfix_str(_status_label(detail.saved, detail.completed), refresh=True)
        progress.close()
        closed_progress_biz.add(biz)

    def observer_factory(account: AccountCredential, is_bulk: bool) -> SyncObserver:
        desc = f'同步 {account.nickname}' if not is_bulk else f'同步 {account.nickname} ({account.biz})'
        progress = tqdm(total=None, desc=desc, unit='msg', dynamic_ncols=True, leave=True)
        progress_map[account.biz] = progress
        return TqdmSyncObserver(progress, account)

    report: SyncReport | None = None
    container = build_sync_container(storage=storage, enable_download=download, enable_images=download)
    async with container as app:
        service = ArticleSyncService(storage=storage, weixin_sync=app.weixin_sync)
        try:
            cli_observer = _CliSyncJobObserver(
                on_done=lambda result, _: close_account_progress(biz=result.biz, detail=result),
            )
            report = await service.sync_accounts(
                accounts=accounts,
                config=config,
                bulk=bulk,
                observer_factory=observer_factory,
                observer=cli_observer,
            )
            if download:
                drained = await app.weixin_sync.drain(limit=CLI_BODY_BATCH)
                report = replace(report, downloaded=report.downloaded + drained.ingested)
                if app.downloader:
                    await app.downloader.wait_for_images()
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
    sleep_seconds: float,
    force: bool,
    skip_time: int | None,
    download: bool = True,
) -> None:
    config = _build_sync_config(
        sleep_seconds=sleep_seconds,
        force=force,
        skip_minutes=skip_time,
        max_pages=pages,
    )
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
                download=download,
            )
            _append_cli_sync_history(
                storage,
                started_at=started_at,
                finished_at=utc_now_iso(),
                status='success',
                saved=report.total_saved,
            )
    typer.echo(f'同步完成，新入队 {report.total_saved} 篇，正文落库 {report.downloaded} 篇')


async def sync_all_accounts(
    *,
    sleep_seconds: float,
    force: bool,
    skip_time: int | None,
    download: bool = True,
) -> None:
    config = _build_sync_config(sleep_seconds=sleep_seconds, force=force, skip_minutes=skip_time)

    with open_storage() as storage:
        accounts = storage.accounts.list_accounts()
        if not accounts:
            typer.echo('尚未保存任何账号，使用 `account add` 添加')
            return

        typer.echo(f'开始同步全部账号（{len(accounts)} 个，列表间隔 {config.sleep_seconds:g} 秒）')
        started_at = utc_now_iso()
        async with _sync_error_handler(storage, started_at=started_at):
            report = await perform_sync(
                storage=storage,
                accounts=accounts,
                config=config,
                bulk=True,
                download=download,
            )
            _append_cli_sync_history(
                storage,
                started_at=started_at,
                finished_at=utc_now_iso(),
                status='success',
                saved=report.total_saved,
            )

    typer.echo(f'全部账号同步完成，新入队 {report.total_saved} 篇，正文落库 {report.downloaded} 篇')


async def sync_group_accounts(
    *,
    group: str,
    sleep_seconds: float,
    force: bool,
    skip_time: int | None,
    download: bool = True,
) -> None:
    config = _build_sync_config(sleep_seconds=sleep_seconds, force=force, skip_minutes=skip_time)

    with open_storage() as storage:
        groups = storage.groups.list_groups()
        if not any(item.name == group for item in groups):
            typer.echo('分组不存在，请先创建分组')
            return
        accounts = storage.accounts.list_accounts(group=group)
        if not accounts:
            typer.echo('分组内暂无账号')
            return

        typer.echo(f'开始同步分组 {group}（{len(accounts)} 个，列表间隔 {config.sleep_seconds:g} 秒）')
        started_at = utc_now_iso()
        async with _sync_error_handler(storage, started_at=started_at):
            report = await perform_sync(
                storage=storage,
                accounts=accounts,
                config=config,
                bulk=True,
                download=download,
            )
            _append_cli_sync_history(
                storage,
                started_at=started_at,
                finished_at=utc_now_iso(),
                status='success',
                saved=report.total_saved,
            )

    typer.echo(f'分组 {group} 同步完成，新入队 {report.total_saved} 篇，正文落库 {report.downloaded} 篇')


__all__ = ['perform_sync', 'sync_account_articles', 'sync_all_accounts', 'sync_group_accounts']
