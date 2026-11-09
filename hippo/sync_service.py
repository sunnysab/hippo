"""Sync scheduling and service orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .container import build_sync_container
from .exceptions import SyncInterrupted
from .logger import get_logger
from .media_settings import get_media_settings
from .models import AccountCredential
from .sources import SourceAdapter, UnsupportedSource, adapter_for
from .storage import PostgresStorage, open_storage
from .sync_core import _get_cancel_event
from .sync_settings import (
    _persist_sync_outcome,
    get_sync_settings,
    set_sync_state,
)
from .sync_types import (
    NullSyncJobObserver,
    NullSyncObserver,
    SyncAccountResult,
    SyncConfig,
    SyncJobObserver,
    SyncObserver,
    SyncReport,
    SyncSummary,
)
from .utils import (
    resolve_sync_interval,
    should_skip_by_interval,
    should_skip_by_time,
    utc_now_iso,
)

logger = get_logger(__name__)

# 列表阶段：账号之间的最小间隔。列表接口和 alias 解析都是敏感操作，
# 2s 那种密度会把 searchcontact 打到限流（2026-09-22 实测：一次全量 alias 校验后
# 该接口连续几十分钟只回 "无法解析公众号标识"）。
LIST_ACCOUNT_MIN_INTERVAL = 15.0
# 同一来源连续失败这么多账号就跳过该来源在本轮的其余账号：多半是被限流，继续打只会更糟。
# 按来源分别计数，微信 daemon 挂掉不会挡住 bilibili/youtube/podcast。
MAX_CONSECUTIVE_LIST_FAILURES = 3
SYNC_RUN_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class SyncJobResult:
    status: dict[str, Any]
    report: SyncReport
    error: str | None


class SyncRunError(RuntimeError):
    def __init__(self, message: str, *, report: SyncReport | None = None) -> None:
        super().__init__(message)
        self.report = report


class ArticleSyncService:
    def __init__(
        self,
        *,
        storage: PostgresStorage,
        adapters: Mapping[str, SourceAdapter],
    ) -> None:
        self._storage = storage
        self._adapters = adapters

    async def sync_account(
        self,
        *,
        account: AccountCredential,
        config: SyncConfig,
        bulk: bool,
        observer: SyncObserver,
        before_request: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[SyncAccountResult, SyncSummary | None]:
        if account.is_disabled:
            observer.on_skip('disabled')
            return (
                SyncAccountResult(
                    biz=account.biz,
                    nickname=account.nickname,
                    source=account.source or 'weixin',
                    saved=0,
                    completed=False,
                    skipped=True,
                    skip_reason='disabled',
                ),
                None,
            )

        if not config.force:
            effective_interval = resolve_sync_interval(account, config.source_intervals_days)
            if should_skip_by_interval(account.last_synced_at, effective_interval):
                observer.on_skip('sync_interval')
                return (
                    SyncAccountResult(
                        biz=account.biz,
                        nickname=account.nickname,
                        source=account.source or 'weixin',
                        saved=0,
                        completed=False,
                        skipped=True,
                        skip_reason='sync_interval',
                    ),
                    None,
                )

        # The short cooldown applies to manual runs too: forcing past the daily
        # interval must not turn into hammering the source on every click.
        if should_skip_by_time(account.last_synced_at, config.skip_minutes):
            observer.on_skip('recently_synced')
            return (
                SyncAccountResult(
                    biz=account.biz,
                    nickname=account.nickname,
                    source=account.source or 'weixin',
                    saved=0,
                    completed=False,
                    skipped=True,
                    skip_reason='recently_synced',
                ),
                None,
            )

        # weixin-rs 数据源：列表阶段只入队（列表接口只有会失效的长链），
        # 正文由队列 drain 统一抓（拿到永久短链 + 原始 HTML 落 article_document）。
        # source_key 优先用缓存好的 gh_id：alias 解析要走 searchcontact，那是个会被限流的窄口。
        source_key = (account.gh_id or account.alias or '').strip()
        if not source_key:
            observer.on_skip('no_source_key')
            return (
                SyncAccountResult(
                    biz=account.biz,
                    nickname=account.nickname,
                    source=account.source or 'weixin',
                    saved=0,
                    completed=False,
                    skipped=True,
                    skip_reason='no_source_key',
                ),
                None,
            )

        try:
            adapter = adapter_for(self._adapters, account.source or 'weixin')
            if before_request is not None:
                await before_request()
            if _get_cancel_event().is_set():
                raise SyncInterrupted('Sync cancelled')
            stats = await adapter.sync_account(
                biz=account.biz,
                source_key=source_key,
                pages=config.max_pages or 1,
            )
        except UnsupportedSource as exc:
            # 没有适配器是配置问题，不是这篇的失败：跳过并说明原因，别把整轮同步拖垮。
            return (
                SyncAccountResult(
                    biz=account.biz,
                    nickname=account.nickname or account.biz,
                    source=account.source or 'weixin',
                    saved=0,
                    completed=False,
                    skipped=True,
                    skip_reason='unsupported_source',
                    error=str(exc),
                ),
                None,
            )
        except SyncInterrupted:
            raise
        except Exception as exc:
            message = str(exc)
            if not bulk:
                raise SyncRunError(message) from exc
            return (
                SyncAccountResult(
                    biz=account.biz,
                    nickname=account.nickname or account.biz,
                    source=account.source or 'weixin',
                    saved=0,
                    completed=False,
                    skipped=False,
                    skip_reason=None,
                    failed=True,
                    error=message,
                ),
                None,
            )

        observer.on_log(f'列表 {stats.listed} 篇，新入队 {stats.enqueued} 条')
        # 列表成功即算「这个账号这一轮同步过了」：sync_interval_days / skip_minutes 都读这个字段
        async with self._storage.transaction():
            await self._storage.accounts.update_last_synced(account.biz)
        return (
            SyncAccountResult(
                biz=account.biz,
                nickname=account.nickname or account.biz,
                source=account.source or 'weixin',
                saved=stats.enqueued,
                completed=True,
                skipped=False,
                skip_reason=None,
            ),
            SyncSummary(total_saved=stats.enqueued, page_count=1, completed=True),
        )

    async def sync_accounts(
        self,
        *,
        accounts: list[AccountCredential],
        config: SyncConfig,
        bulk: bool,
        observer_factory: Callable[[AccountCredential, bool], SyncObserver] | None = None,
        observer: SyncJobObserver | None = None,
    ) -> SyncReport:
        job_observer = observer or NullSyncJobObserver()
        accounts_total = len(accounts)
        total_saved = 0
        failed_accounts = 0
        summary_rows: list[tuple[str, int]] = []
        details: list[SyncAccountResult] = []

        async def _build_report(*, current_account: AccountCredential | None = None) -> SyncReport:
            current = None
            if current_account is not None:
                current = {
                    'biz': current_account.biz,
                    'nickname': current_account.nickname or current_account.biz,
                }
            return SyncReport(
                total_saved=total_saved,
                summary=list(summary_rows),
                details=list(details),
                downloaded=0,
                failed_accounts=failed_accounts,
                accounts_total=accounts_total,
                accounts_done=len(details),
                current_account=current,
            )

        consecutive_failures: dict[str, int] = {}
        blocked_sources: set[str] = set()
        last_request_at: dict[str, float] = {}
        for account in accounts:
            if _get_cancel_event().is_set():
                break
            account_source = account.source or 'weixin'
            if account_source in blocked_sources:
                # Rate limiting is per source: a WeChat outage must not stop the
                # media sources from syncing in the same round. The account is
                # still reported as skipped, or the progress would stop short of
                # the total and the detail list would silently lose rows.
                blocked_result = SyncAccountResult(
                    biz=account.biz,
                    nickname=account.nickname or account.biz,
                    source=account_source,
                    saved=0,
                    completed=False,
                    skipped=True,
                    skip_reason='source_blocked',
                )
                details.append(blocked_result)
                job_observer.on_account_done(blocked_result, None)
                continue

            async def before_request(source: str = account_source) -> None:
                loop = asyncio.get_running_loop()
                previous = last_request_at.get(source)
                wait = max(0.0, config.sleep_seconds - (loop.time() - previous)) if previous is not None else 0.0
                if wait:
                    job_observer.on_log(f'列表请求间隔，等待 {wait:g} 秒')
                    await asyncio.sleep(wait)
                last_request_at[source] = loop.time()

            job_observer.on_account_start(account)
            job_observer.on_account_stage(account, 'listing')
            page_observer = observer_factory(account, bulk) if observer_factory else NullSyncObserver()
            try:
                result, summary = await self.sync_account(
                    account=account,
                    config=config,
                    bulk=bulk,
                    observer=page_observer,
                    before_request=before_request,
                )
            except SyncRunError as exc:
                raise SyncRunError(str(exc), report=await _build_report(current_account=account)) from exc
            except SyncInterrupted:
                cancelled_result = SyncAccountResult(
                    biz=account.biz,
                    nickname=account.nickname or account.biz,
                    source=account.source or 'weixin',
                    saved=0,
                    completed=False,
                    skipped=False,
                    skip_reason=None,
                    failed=False,
                    error=None,
                )
                job_observer.on_account_done(cancelled_result, None)
                break
            details.append(result)
            if result.failed:
                failed_accounts += 1
                failures = consecutive_failures.get(account_source, 0) + 1
                consecutive_failures[account_source] = failures
                job_observer.on_account_done(result, summary)
                if failures >= MAX_CONSECUTIVE_LIST_FAILURES:
                    blocked_sources.add(account_source)
                    job_observer.on_log(
                        f'{account_source}: 连续 {failures} 个账号失败，跳过该来源其余账号（可能被限流），留到下一轮'
                    )
                continue
            consecutive_failures[account_source] = 0
            if result.skipped and not bulk:
                job_observer.on_account_done(result, summary)
                return SyncReport(
                    total_saved=0,
                    summary=[],
                    details=details,
                    downloaded=0,
                    failed_accounts=failed_accounts,
                    accounts_total=accounts_total,
                    accounts_done=len(details),
                )
            if result.skipped:
                job_observer.on_account_done(result, summary)
                continue

            if summary:
                total_saved += summary.total_saved
                summary_rows.append((result.nickname or result.biz, summary.total_saved))

            job_observer.on_account_done(result, summary)

        return SyncReport(
            total_saved=total_saved,
            summary=summary_rows,
            details=details,
            downloaded=0,
            failed_accounts=failed_accounts,
            accounts_total=accounts_total,
            accounts_done=len(details),
        )


def _build_sync_config(settings: dict[str, Any], *, force: bool = False) -> SyncConfig:
    return SyncConfig(
        # 账号间请求间隔：给个下限，避免历史配置里的 2s 继续把接口打限流
        sleep_seconds=max(float(settings.get('sleep_seconds') or 0), LIST_ACCOUNT_MIN_INTERVAL),
        # Manual runs force past the per-account daily interval; the short
        # ``skip_minutes`` cooldown is applied separately and still holds.
        force=force,
        skip_minutes=settings.get('skip_minutes'),
        source_intervals_days=settings.get('source_intervals_days', {}),
    )


async def run_sync_job(
    *,
    group_id: int | None = None,
    biz_list: list[str] | None = None,
    source: str | None = None,
    force: bool = False,
    observer_factory: Callable[[AccountCredential, bool], SyncObserver] | None = None,
    observer: SyncJobObserver | None = None,
    lock: asyncio.Lock | None = None,
    scope_sources: tuple[str, ...] | None = None,
    source_slots: asyncio.Semaphore | None = None,
) -> SyncJobResult:
    job_observer = observer or NullSyncJobObserver()
    if lock:
        async with lock:
            job_observer.on_lock_acquired()
            return await run_sync_job(
                group_id=group_id,
                biz_list=biz_list,
                source=source,
                force=force,
                observer_factory=observer_factory,
                observer=job_observer,
                lock=None,
                scope_sources=scope_sources,
                source_slots=source_slots,
            )
    job_observer.on_lock_acquired()
    started_at = utc_now_iso()
    empty_report = SyncReport(total_saved=0, summary=[], details=[], downloaded=0)
    error: str | None = None
    report = empty_report
    try:
        # Release configuration reads before source tasks wait for connections.
        async with open_storage() as storage:
            settings = await get_sync_settings(storage)
            await set_sync_state(storage, status='running', error='', started_at=started_at)
            media_settings = await get_media_settings(storage)
            accounts = await storage.accounts.list_syncable_accounts(
                group_ids=[group_id] if group_id is not None else None,
                biz_list=biz_list,
                source=source,
                source_intervals_days=settings.get('source_intervals_days', {}),
            )
        if scope_sources is not None:
            accounts = [account for account in accounts if (account.source or 'weixin') in scope_sources]
        job_observer.on_accounts_loaded(accounts)
        source_groups: dict[str, list[AccountCredential]] = {}
        for account in accounts:
            source_groups.setdefault(account.source or 'weixin', []).append(account)

        async def sync_source(selected: list[AccountCredential]) -> tuple[SyncReport, str | None]:
            try:
                async with open_storage() as source_storage:
                    container = build_sync_container(
                        storage=source_storage,
                        enable_download=False,
                        enable_images=bool(settings.get('download_images')),
                        media_settings=media_settings,
                        sources={selected[0].source or 'weixin'},
                    )
                    async with container as app:
                        service = ArticleSyncService(storage=source_storage, adapters=app.adapters)
                        source_report = await service.sync_accounts(
                            accounts=selected,
                            config=_build_sync_config(settings, force=force),
                            bulk=True,
                            observer_factory=observer_factory,
                            observer=job_observer,
                        )
                        return source_report, None
            except SyncRunError as exc:
                return exc.report or empty_report, str(exc)
            except Exception as exc:
                logger.exception('Source listing failed')
                return empty_report, str(exc)

        async def limited_source(selected: list[AccountCredential]) -> tuple[SyncReport, str | None]:
            if source_slots is None:
                return await sync_source(selected)
            async with source_slots:
                return await sync_source(selected)

        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(limited_source(selected)) for selected in source_groups.values()]
        outcomes = [task.result() for task in tasks]
        reports = [item[0] for item in outcomes]
        error = '; '.join(item[1] for item in outcomes if item[1]) or None
        report = SyncReport(
            total_saved=sum(item.total_saved for item in reports),
            summary=[row for item in reports for row in item.summary],
            details=[row for item in reports for row in item.details],
            downloaded=sum(item.downloaded for item in reports),
            failed_accounts=sum(item.failed_accounts for item in reports),
            accounts_total=len(accounts),
            accounts_done=sum(item.accounts_done for item in reports),
        )
        if _get_cancel_event().is_set():
            error = 'Cancelled by user'
    except Exception as exc:
        logger.exception('Sync job failed unexpectedly')
        error = str(exc)
        report = empty_report

    finished_at = utc_now_iso()
    for attempt in range(2):
        try:
            async with open_storage() as storage:
                status = await _persist_sync_outcome(
                    storage,
                    started_at=started_at,
                    finished_at=finished_at,
                    error=error,
                    report=report,
                )
            return SyncJobResult(status=status, report=report, error=error)
        except Exception:
            logger.exception('Failed to persist sync outcome (attempt %d)', attempt + 1)
    return SyncJobResult(status={'status': 'failed'}, report=report, error=error or 'failed_to_persist_sync_outcome')


__all__ = [
    'SYNC_RUN_LOCK',
    'ArticleSyncService',
    'SyncJobResult',
    'SyncRunError',
    'run_sync_job',
]
