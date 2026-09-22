"""Sync scheduling and service orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from .container import build_sync_container
from .exceptions import SyncInterrupted
from .file_storage import FileStorageError
from .models import AccountCredential
from .storage import PostgresStorage, open_storage
from .sync_core import _get_cancel_event, reset_sync_cancel
from .sync_settings import (
    _persist_sync_outcome,
    get_sync_settings,
    get_sync_status,
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
from .weixin_worker import WeixinArticleSync

logger = logging.getLogger('hippo.sync')

# 列表阶段：账号之间的最小间隔。列表接口和 alias 解析都是敏感操作，
# 2s 那种密度会把 searchcontact 打到限流（2026-09-22 实测：一次全量 alias 校验后
# 该接口连续几十分钟只回 "无法解析公众号标识"）。
LIST_ACCOUNT_MIN_INTERVAL = 15.0
# 连续失败这么多账号就中止本轮：多半是被限流，继续打只会更糟
MAX_CONSECUTIVE_LIST_FAILURES = 3
# 一次 sync job 只顺带抓一批正文；持续抓是 worker 里常驻 drain 循环的活
JOB_BODY_BATCH = 5

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
        weixin_sync: WeixinArticleSync,
    ) -> None:
        self._storage = storage
        self._weixin_sync = weixin_sync

    async def sync_account(
        self,
        *,
        account: AccountCredential,
        config: SyncConfig,
        bulk: bool,
        observer: SyncObserver,
    ) -> tuple[SyncAccountResult, SyncSummary | None]:
        if account.is_disabled:
            observer.on_skip('disabled')
            return (
                SyncAccountResult(
                    biz=account.biz,
                    nickname=account.nickname,
                    saved=0,
                    completed=False,
                    skipped=True,
                    skip_reason='disabled',
                ),
                None,
            )

        if not config.force:
            effective_interval = resolve_sync_interval(self._storage, account)
            if should_skip_by_interval(account.last_synced_at, effective_interval, account.biz):
                observer.on_skip('sync_interval')
                return (
                    SyncAccountResult(
                        biz=account.biz,
                        nickname=account.nickname,
                        saved=0,
                        completed=False,
                        skipped=True,
                        skip_reason='sync_interval',
                    ),
                    None,
                )

            if should_skip_by_time(account.last_synced_at, config.skip_minutes):
                observer.on_skip('recently_synced')
                return (
                    SyncAccountResult(
                        biz=account.biz,
                        nickname=account.nickname,
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
                    saved=0,
                    completed=False,
                    skipped=True,
                    skip_reason='no_source_key',
                ),
                None,
            )

        try:
            stats = await self._weixin_sync.sync_account(
                biz=account.biz,
                source_key=source_key,
                pages=config.max_pages or 1,
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
        with self._storage.transaction():
            self._storage.accounts.update_last_synced(account.biz)
        return (
            SyncAccountResult(
                biz=account.biz,
                nickname=account.nickname or account.biz,
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

        def _build_report(*, current_account: AccountCredential | None = None) -> SyncReport:
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

        consecutive_failures = 0
        for i, account in enumerate(accounts):
            if _get_cancel_event().is_set():
                break
            if i > 0:
                job_observer.on_log(f'列表请求间隔，等待 {config.sleep_seconds:g} 秒')
                await asyncio.sleep(config.sleep_seconds)
            job_observer.on_account_start(account)
            job_observer.on_account_stage(account, 'listing')
            page_observer = observer_factory(account, bulk) if observer_factory else NullSyncObserver()
            try:
                result, summary = await self.sync_account(
                    account=account,
                    config=config,
                    bulk=bulk,
                    observer=page_observer,
                )
            except SyncRunError as exc:
                raise SyncRunError(str(exc), report=_build_report(current_account=account)) from exc
            except SyncInterrupted:
                cancelled_result = SyncAccountResult(
                    biz=account.biz,
                    nickname=account.nickname or account.biz,
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
                consecutive_failures += 1
                job_observer.on_account_done(result, summary)
                if consecutive_failures >= MAX_CONSECUTIVE_LIST_FAILURES:
                    job_observer.on_log(
                        f'连续 {consecutive_failures} 个账号失败，中止本轮（可能被限流），留到下一轮'
                    )
                    break
                continue
            consecutive_failures = 0
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


def _build_sync_config(settings: dict[str, Any]) -> SyncConfig:
    return SyncConfig(
        # 账号间请求间隔：给个下限，避免历史配置里的 2s 继续把接口打限流
        sleep_seconds=max(float(settings.get('sleep_seconds') or 0), LIST_ACCOUNT_MIN_INTERVAL),
        force=False,
        skip_minutes=settings.get('skip_minutes'),
    )


async def run_sync_job(
    *,
    group_id: int | None = None,
    biz_list: list[str] | None = None,
    observer_factory: Callable[[AccountCredential, bool], SyncObserver] | None = None,
    observer: SyncJobObserver | None = None,
    lock: asyncio.Lock | None = None,
) -> SyncJobResult:
    job_observer = observer or NullSyncJobObserver()
    if lock:
        async with lock:
            job_observer.on_lock_acquired()
            return await run_sync_job(
                group_id=group_id,
                biz_list=biz_list,
                observer_factory=observer_factory,
                observer=job_observer,
                lock=None,
            )
    job_observer.on_lock_acquired()
    reset_sync_cancel()
    started_at = utc_now_iso()
    empty_report = SyncReport(total_saved=0, summary=[], details=[], downloaded=0)
    with open_storage() as storage:
        settings = get_sync_settings(storage)
        set_sync_state(storage, status='running', error='', started_at=started_at)

        error: str | None = None
        report = empty_report
        accounts: list[AccountCredential] = []
        try:
            accounts = storage.accounts.list_accounts()
            if group_id is not None:
                accounts = [account for account in accounts if account.group_id == group_id]
            if biz_list is not None:
                allowed_biz = set(biz_list)
                accounts = [account for account in accounts if account.biz in allowed_biz]
            job_observer.on_accounts_loaded(accounts)

            container = None
            try:
                container = build_sync_container(
                    storage=storage,
                    enable_download=bool(settings.get('download_content')),
                    enable_images=bool(settings.get('download_images')),
                )
            except FileStorageError as exc:
                error = str(exc)

            if error is None and container:
                async with container as app:
                    config = _build_sync_config(settings)
                    service = ArticleSyncService(
                        storage=storage,
                        weixin_sync=app.weixin_sync,
                    )
                    try:
                        report = await service.sync_accounts(
                            accounts=accounts,
                            config=config,
                            bulk=True,
                            observer_factory=observer_factory,
                            observer=job_observer,
                        )
                    except SyncRunError as exc:
                        error = str(exc)
                        report = exc.report or empty_report
                    except Exception as exc:
                        error = str(exc)
                        report = empty_report
                    # 列表入队完，接着把正文抓下来（节流在 daemon 侧）
                    if error is None and settings.get('download_content'):
                        job_observer.on_log('正文阶段：处理待抓队列')
                        drained = await app.weixin_sync.drain(limit=settings.get('content_limit') or JOB_BODY_BATCH)
                        job_observer.on_log(
                            f'正文落库 {drained.ingested} 篇，失败 {drained.failed} 篇'
                        )
                        report = replace(report, downloaded=report.downloaded + drained.ingested)
                    if _get_cancel_event().is_set() and not error:
                        error = 'Cancelled by user'
                    if settings.get('download_images') and app.downloader:
                        job_observer.on_images_start()
                        await app.downloader.wait_for_images()
                        job_observer.on_images_done()
        except Exception as exc:
            logger.exception('Sync job failed unexpectedly')
            error = str(exc)
            report = empty_report

        try:
            storage.rollback()
        except Exception as exc:
            logger.warning('Failed to rollback storage connection: %s', exc)

        finished_at = utc_now_iso()
        try:
            status = _persist_sync_outcome(
                storage,
                started_at=started_at,
                finished_at=finished_at,
                error=error,
                report=report,
            )
            return SyncJobResult(status=status, report=report, error=error)
        except Exception:
            logger.exception('Failed to persist sync outcome; retrying with a fresh connection.')
            try:
                with open_storage() as retry_storage:
                    with contextlib.suppress(Exception):
                        retry_storage.rollback()
                    status = _persist_sync_outcome(
                        retry_storage,
                        started_at=started_at,
                        finished_at=finished_at,
                        error=error or 'failed_to_persist_sync_outcome',
                        report=report,
                    )
                return SyncJobResult(status=status, report=report, error=error)
            except Exception:
                logger.exception('Failed to persist sync outcome with a fresh connection.')
                return SyncJobResult(status=get_sync_status(storage), report=report, error=error or 'failed')


__all__ = [
    'SYNC_RUN_LOCK',
    'ArticleSyncService',
    'SyncJobResult',
    'SyncRunError',
    'run_sync_job',
]
