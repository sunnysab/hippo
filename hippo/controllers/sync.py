"""Sync controller for account article synchronization."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Awaitable, Callable

import typer
from tqdm import tqdm

from ..http import MPClient
from ..models import AccountCredential, LoginSession
from ..storage import StorageLike, open_storage
from ..sync_core import SyncInterrupted, sync_account_core


class SyncMode(str, Enum):
    full = 'full'
    incremental = 'incremental'
    recent = 'recent'
    range = 'range'

    def __str__(self) -> str:  # pragma: no cover - click displays value
        return self.value



@dataclass(frozen=True)
class SyncOptions:
    mode: SyncMode
    page_size: int
    pages: int | None
    sleep_seconds: float
    reset: bool
    recent_days: int | None
    since_date: str | None
    until_date: str | None
    force: bool
    skip_time: int | None


@dataclass(frozen=True)
class SyncPlan:
    page_limit: int | None
    since_timestamp: int | None
    until_timestamp: int | None
    stop_on_existing: bool
    full_synced_hint: bool
    resume_key: str | None
    complete_key: str | None


@dataclass(frozen=True)
class SyncReport:
    total_saved: int
    summary: list[tuple[str, int]]


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


def _enforce_exclusive_flags(force: bool, skip_minutes: int | None) -> None:
    if force and skip_minutes is not None:
        raise typer.BadParameter('--force 与 --skip-time 不能同时使用')


def _should_skip_by_time(last_synced_at: datetime | None, skip_minutes: int | None) -> bool:
    if skip_minutes is None or not last_synced_at:
        return False
    threshold = datetime.now(timezone.utc) - timedelta(minutes=skip_minutes)
    if last_synced_at.tzinfo is None:
        last_synced_at = last_synced_at.replace(tzinfo=timezone.utc)
    else:
        last_synced_at = last_synced_at.astimezone(timezone.utc)
    return last_synced_at >= threshold


def _format_last_synced(last_synced_at: datetime | None) -> str:
    return last_synced_at.isoformat(timespec='seconds') if last_synced_at else '-'


def _to_utc_timestamp(value: datetime | None) -> int | None:
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return int(value.timestamp())


def _today_str() -> str:
    return date.today().isoformat()


def _format_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return ''
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    sep = '  '
    lines = [sep.join(h.ljust(widths[idx]) for idx, h in enumerate(headers))]
    lines.append(sep.join('-' * widths[idx] for idx in range(len(headers))))
    for row in rows:
        lines.append(sep.join(row[idx].ljust(widths[idx]) for idx in range(len(headers))))
    return '\n'.join(lines)


def _pbar_write(progress: tqdm | None, message: str) -> None:
    if progress is not None:
        progress.write(message)
    else:
        typer.echo(message)


def _handle_login_expired() -> bool:
    typer.echo('登录状态可能已失效，请先运行 `hippo login` 后重试同步。')
    return False


def _get_login_session(storage: StorageLike) -> LoginSession:
    try:
        return storage.get_login_session()
    except LookupError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1)


def _resolve_shared_window(options: SyncOptions) -> tuple[int | None, int | None]:
    if options.mode == SyncMode.recent:
        if options.recent_days is None:
            raise typer.BadParameter('--recent-days is required for --mode recent.')
        since = int((datetime.now(timezone.utc) - timedelta(days=options.recent_days)).timestamp())
        return since, None
    if options.mode == SyncMode.range:
        if not options.since_date:
            raise typer.BadParameter('--since is required for --mode range.')
        since = _parse_sync_date(options.since_date, label='--since')
        until = _parse_sync_date(options.until_date, label='--until', end_of_day=True)
        if until is not None and since is not None and until < since:
            raise typer.BadParameter('--until must be on or after --since.')
        return since, until
    return None, None


def _build_sync_plan(
    *,
    storage: StorageLike,
    account: AccountCredential,
    options: SyncOptions,
    shared_since: int | None,
    shared_until: int | None,
    bulk: bool,
) -> SyncPlan:
    since_timestamp = None
    until_timestamp = None
    stop_on_existing = False
    full_synced_hint = False
    page_limit = options.pages if not bulk else None
    resume_key = None
    complete_key = None

    if bulk:
        resume_key = f'sync_progress:{account.biz}'
        complete_key = f'sync_complete:{account.biz}'

    if options.mode == SyncMode.full:
        page_limit = None
        full_synced_hint = storage.get_meta(f'sync_complete:{account.biz}') is not None
    elif options.mode == SyncMode.incremental:
        since_timestamp = _to_utc_timestamp(account.last_synced_at)
        if since_timestamp is None:
            stop_on_existing = True
    elif options.mode == SyncMode.recent:
        since_timestamp = shared_since
    elif options.mode == SyncMode.range:
        since_timestamp = shared_since
        until_timestamp = shared_until

    if bulk and options.mode != SyncMode.full:
        resume_key = None
        full_synced_hint = False

    if options.mode in (SyncMode.incremental, SyncMode.recent, SyncMode.range):
        full_synced_hint = False

    return SyncPlan(
        page_limit=page_limit,
        since_timestamp=since_timestamp,
        until_timestamp=until_timestamp,
        stop_on_existing=stop_on_existing,
        full_synced_hint=full_synced_hint,
        resume_key=resume_key if options.mode == SyncMode.full and bulk else None,
        complete_key=complete_key,
    )


async def _sync_account_pages(
    *,
    storage: StorageLike,
    client: MPClient,
    account: AccountCredential,
    page_size: int,
    pages: int | None,
    sleep_seconds: float,
    resume_key: str | None = None,
    full_synced_hint: bool = False,
    since_timestamp: int | None = None,
    until_timestamp: int | None = None,
    stop_on_existing: bool = False,
    progress: tqdm | None = None,
    login_flow: Callable[..., Awaitable[None]] | None = None,
) -> tuple[int, int, bool]:
    total_saved = 0
    page_count = 0
    completed = False
    try:
        async for event, payload in sync_account_core(
            storage=storage,
            client=client,
            account=account,
            page_size=page_size,
            pages=pages,
            sleep_seconds=sleep_seconds,
            resume_key=resume_key,
            full_synced_hint=full_synced_hint,
            since_timestamp=since_timestamp,
            until_timestamp=until_timestamp,
            stop_on_existing=stop_on_existing,
            login_flow=login_flow,
            on_login_required=_handle_login_expired,
        ):
            if event == "log":
                _pbar_write(progress, str(payload))
            elif event == "progress" and progress is not None:
                total = payload.get("total")
                current = payload.get("current")
                delta = payload.get("delta", 0)
                if total and total > 0 and progress.total != total:
                    progress.total = total
                if delta:
                    progress.update(delta)
                elif current is not None:
                    progress.n = current
                    progress.refresh()
            elif event == "complete":
                total_saved = int(payload.get("total_saved", 0))
                page_count = int(payload.get("page_count", 0))
                completed = bool(payload.get("completed"))
    except SyncInterrupted:
        message = f'检测到中断，已保存断点：{account.nickname}'
        _pbar_write(progress, message)
        raise
    return total_saved, page_count, completed


async def perform_sync(
    *,
    storage: StorageLike,
    accounts: list[AccountCredential],
    options: SyncOptions,
    bulk: bool,
    login_flow: Callable[..., Awaitable[None]] | None,
) -> SyncReport:
    _enforce_exclusive_flags(options.force, options.skip_time)
    shared_since, shared_until = _resolve_shared_window(options)
    total_saved = 0
    summary: list[tuple[str, int]] = []

    async with MPClient() as client:
        for account in accounts:
            resume_key = f'sync_progress:{account.biz}' if bulk else None
            complete_key = f'sync_complete:{account.biz}' if bulk else None
            if bulk and options.mode == SyncMode.full and options.reset:
                if resume_key:
                    storage.delete_meta(resume_key)
                if complete_key:
                    storage.delete_meta(complete_key)

            if account.is_disabled:
                if bulk:
                    progress = tqdm(
                        total=0,
                        desc=f'同步 {account.nickname} ({account.biz})',
                        unit='msg',
                        dynamic_ncols=True,
                        leave=True,
                    )
                    progress.set_postfix_str('skipped (disabled)', refresh=True)
                    progress.close()
                    continue
                typer.echo(f'Account {account.nickname} ({account.biz}) is disabled. Skipping.')
                return SyncReport(total_saved=0, summary=[])

            if (
                bulk
                and options.mode == SyncMode.full
                and options.skip_time is None
                and not options.force
                and complete_key
                and storage.get_meta(complete_key) == _today_str()
            ):
                progress = tqdm(
                    total=0,
                    desc=f'同步 {account.nickname} ({account.biz})',
                    unit='msg',
                    dynamic_ncols=True,
                    leave=True,
                )
                progress.set_postfix_str('跳过(今日已完成)', refresh=True)
                progress.close()
                continue

            if not options.force and _should_skip_by_time(account.last_synced_at, options.skip_time):
                last_synced = _format_last_synced(account.last_synced_at)
                if bulk:
                    progress = tqdm(
                        total=0,
                        desc=f'同步 {account.nickname} ({account.biz})',
                        unit='msg',
                        dynamic_ncols=True,
                        leave=True,
                    )
                    progress.set_postfix_str(f'跳过(近期已同步 {last_synced})', refresh=True)
                    progress.close()
                    continue
                typer.echo(f'该账号近期已同步，跳过（上次同步 {last_synced}）')
                return SyncReport(total_saved=0, summary=[])

            plan = _build_sync_plan(
                storage=storage,
                account=account,
                options=options,
                shared_since=shared_since,
                shared_until=shared_until,
                bulk=bulk,
            )

            progress_desc = f'同步 {account.nickname}' if not bulk else f'同步 {account.nickname} ({account.biz})'
            progress = tqdm(
                total=None,
                desc=progress_desc,
                unit='msg',
                dynamic_ncols=True,
                leave=True,
            )
            try:
                saved, _, completed = await _sync_account_pages(
                    storage=storage,
                    client=client,
                    account=account,
                    page_size=options.page_size,
                    pages=plan.page_limit,
                    sleep_seconds=options.sleep_seconds,
                    resume_key=plan.resume_key,
                    full_synced_hint=plan.full_synced_hint,
                    since_timestamp=plan.since_timestamp,
                    until_timestamp=plan.until_timestamp,
                    stop_on_existing=plan.stop_on_existing,
                    progress=progress,
                    login_flow=login_flow,
                )
                status = '成功' if completed else '未完成'
                if completed and saved == 0:
                    status = '已是最新'
                progress.set_postfix_str(status, refresh=True)
                if completed and bulk and options.mode == SyncMode.full and plan.complete_key:
                    storage.set_meta(plan.complete_key, _today_str())
            except SyncInterrupted:
                progress.set_postfix_str('未完成', refresh=True)
                typer.echo('同步中断，断点已保存')
                raise typer.Exit(code=130)
            except RuntimeError as exc:
                progress.set_postfix_str('失败', refresh=True)
                typer.echo(f'同步失败：{exc}')
                raise typer.Exit(code=1)
            finally:
                progress.close()

            total_saved += saved
            summary.append((account.nickname or account.biz, saved))

    return SyncReport(total_saved=total_saved, summary=summary)


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
    options = SyncOptions(
        mode=mode,
        page_size=page_size,
        pages=pages,
        sleep_seconds=0,
        reset=False,
        recent_days=recent_days,
        since_date=since_date,
        until_date=until_date,
        force=force,
        skip_time=skip_time,
    )
    with open_storage() as storage:
        account = storage.get_account(biz)
        typer.echo(f'开始同步 {account.nickname} 的文章')
        report = await perform_sync(
            storage=storage,
            accounts=[account],
            options=options,
            bulk=False,
            login_flow=login_flow,
        )
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
    options = SyncOptions(
        mode=mode,
        page_size=page_size,
        pages=None,
        sleep_seconds=sleep_seconds,
        reset=reset,
        recent_days=recent_days,
        since_date=since_date,
        until_date=until_date,
        force=force,
        skip_time=skip_time,
    )

    with open_storage() as storage:
        accounts = storage.list_accounts()
        if not accounts:
            typer.echo('尚未保存任何账号，使用 `account add` 添加')
            return

        header = '开始同步全部账号（从最新文章往更早翻页）'
        if reset:
            header = '开始同步全部账号（重置断点，从最新文章往更早翻页）'
        if sleep_seconds > 0:
            header += f' 每页间隔 {sleep_seconds} 秒'
        typer.echo(header)

        report = await perform_sync(
            storage=storage,
            accounts=accounts,
            options=options,
            bulk=True,
            login_flow=login_flow,
        )

    if report.summary:
        headers = ['账号', '新增/更新']
        rows = [[name, str(saved)] for name, saved in report.summary]
        table_text = _format_table(headers, rows)
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
    options = SyncOptions(
        mode=mode,
        page_size=page_size,
        pages=None,
        sleep_seconds=sleep_seconds,
        reset=reset,
        recent_days=recent_days,
        since_date=since_date,
        until_date=until_date,
        force=force,
        skip_time=skip_time,
    )

    with open_storage() as storage:
        groups = storage.list_groups()
        target = next((item for item in groups if item.name == group), None)
        if not target:
            typer.echo('分组不存在，请先创建分组')
            return
        accounts = storage.list_accounts(group=group)
        if not accounts:
            typer.echo('分组内暂无账号')
            return

        header = f'开始同步分组 {group}（从最新文章往更早翻页）'
        if reset:
            header = f'开始同步分组 {group}（重置断点，从最新文章往更早翻页）'
        if sleep_seconds > 0:
            header += f' 每页间隔 {sleep_seconds} 秒'
        typer.echo(header)

        report = await perform_sync(
            storage=storage,
            accounts=accounts,
            options=options,
            bulk=True,
            login_flow=login_flow,
        )

    if report.summary:
        headers = ['账号', '新增/更新']
        rows = [[name, str(saved)] for name, saved in report.summary]
        table_text = _format_table(headers, rows)
        if table_text:
            typer.echo(table_text)
    typer.echo(f'分组 {group} 同步完成，共写入 {report.total_saved} 条记录')


__all__ = ['SyncMode', 'sync_account_articles', 'sync_all_accounts', 'sync_group_accounts']
