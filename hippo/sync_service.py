"""Sync scheduling and service orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import DEFAULT_PAGE_SIZE, DEFAULT_RECENT_DAYS
from .container import build_sync_container
from .downloader import ArticleDownloader
from .file_storage import FileStorageError
from .models import AccountCredential, ArticleRecord
from .storage import PostgresStorage, fetchall_rows, open_storage
from .exceptions import SyncInterrupted
from .sync_core import (
    _get_cancel_event,
    is_freq_control,
    is_login_error,
    reset_sync_cancel,
    sync_account_core,
)
from .sync_settings import (
    ALERT_SENT_KEY,
    SYNC_LOGIN_REQUIRED_AT_KEY,
    _get_window_hours,
    _is_within_sync_window,
    _persist_sync_outcome,
    _should_skip_for_login,
    _to_utc_timestamp,
    _today_str,
    append_sync_history,
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
    SyncMode,
    SyncObserver,
    SyncPlan,
    SyncReport,
    SyncSummary,
)
from .utils import parse_iso_date_to_timestamp, should_skip_by_time, utc_now_iso
from .wechat_api import WeChatApiClient

logger = logging.getLogger('hippo.sync')

SYNC_RUN_LOCK = asyncio.Lock()


async def _run_backfill_images() -> None:
    try:
        from .image_backfill import backfill_article_images
    except Exception:
        return
    try:
        await backfill_article_images()
    except Exception:
        logger.exception('Background image backfill failed.')


@dataclass(frozen=True)
class SyncJobResult:
    status: dict[str, Any]
    report: SyncReport
    error: str | None


class SyncRunError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        login_required: bool = False,
        report: SyncReport | None = None,
    ) -> None:
        super().__init__(message)
        self.login_required = login_required
        self.report = report


class PageCollector(NullSyncObserver):
    def __init__(self) -> None:
        self.records: list[ArticleRecord] = []

    def on_page(self, payload: dict[str, Any]) -> None:
        records = payload.get('records') or []
        existing_ids = payload.get('existing_ids') or set()
        if not records:
            return
        if existing_ids:
            filtered = [record for record in records if record.article_id not in existing_ids]
            self.records.extend(filtered)
            return
        self.records.extend(records)


def _parse_date(value: str | None, *, end_of_day: bool = False) -> int | None:
    if not value:
        return None
    try:
        return parse_iso_date_to_timestamp(value, end_of_day=end_of_day)
    except ValueError as exc:
        raise ValueError(f'Invalid date: {value}') from exc


def _select_missing_content(
    storage: PostgresStorage,
    biz: str,
    *,
    limit: int | None,
) -> list[ArticleRecord]:
    if limit is not None and limit <= 0:
        return []
    articles = storage.articles.list_articles(biz, limit=limit)
    try:
        ids = storage.articles.get_article_content_ids(biz, [article.article_id for article in articles])
        return [article for article in articles if article.article_id not in ids]
    except Exception as exc:
        with contextlib.suppress(Exception):
            storage.rollback()
        logger.warning('Failed to query article content IDs (biz=%s): %s', biz, exc)
        return articles


class ArticleSyncService:
    def __init__(
        self,
        *,
        storage: PostgresStorage,
        client: WeChatApiClient,
        downloader: ArticleDownloader | None = None,
        login_flow: Callable[..., Any] | None = None,
        on_login_required: Callable[[], bool] | None = None,
    ) -> None:
        self._storage = storage
        self._client = client
        self._downloader = downloader
        self._login_flow = login_flow
        self._on_login_required = on_login_required

    def _resolve_shared_window(self, config: SyncConfig) -> tuple[int | None, int | None]:
        if config.mode == SyncMode.recent:
            if config.recent_days is None:
                raise ValueError('--recent-days is required for recent mode.')
            since = int((datetime.now(UTC) - timedelta(days=config.recent_days)).timestamp())
            return since, None
        if config.mode == SyncMode.range:
            if not config.since_date:
                raise ValueError('--since is required for range mode.')
            since = _parse_date(config.since_date)
            until = _parse_date(config.until_date, end_of_day=True)
            if until is not None and since is not None and until < since:
                raise ValueError('--until must be on or after --since.')
            return since, until
        return None, None

    def _resolve_account_mode(
        self,
        *,
        account: AccountCredential,
        config: SyncConfig,
        group_defaults: dict[int, dict[str, Any]] | None,
    ) -> tuple[SyncMode, int | None]:
        if config.mode is not None:
            return config.mode, config.recent_days
        group_sync = None
        if group_defaults and account.group_id is not None:
            group_sync = group_defaults.get(account.group_id)
        group_mode = group_sync.get('sync_mode') if group_sync else None
        group_recent_days = group_sync.get('sync_recent_days') if group_sync else None
        mode_value = (account.sync_mode or group_mode or SyncMode.incremental.value).strip().lower()
        try:
            mode = SyncMode(mode_value)
        except ValueError:
            mode = SyncMode.incremental
        recent_days = account.sync_recent_days
        if recent_days is None:
            recent_days = group_recent_days if group_recent_days is not None else DEFAULT_RECENT_DAYS
        return mode, recent_days

    def _build_sync_plan(
        self,
        *,
        account: AccountCredential,
        config: SyncConfig,
        mode: SyncMode,
        recent_days: int | None,
        shared_since: int | None,
        shared_until: int | None,
        use_resume: bool,
        bulk: bool,
    ) -> SyncPlan:
        since_timestamp = None
        until_timestamp = None
        stop_on_existing = False
        full_synced_hint = False
        resume_key = None
        complete_key = None

        if use_resume and bulk:
            resume_key = f'sync_progress:{account.biz}'
            complete_key = f'sync_complete:{account.biz}'

        if mode == SyncMode.full:
            full_synced_hint = self._storage.meta.get(f'sync_complete:{account.biz}') is not None
        elif mode == SyncMode.incremental:
            since_timestamp = _to_utc_timestamp(account.last_synced_at)
            if since_timestamp is None:
                stop_on_existing = True
        elif mode == SyncMode.recent:
            if config.mode is not None:
                since_timestamp = shared_since
            else:
                days = max(int(recent_days or 1), 1)
                since_timestamp = int(datetime.now(UTC).timestamp() - days * 86400)
        elif mode == SyncMode.range:
            if config.mode is not None:
                since_timestamp = shared_since
                until_timestamp = shared_until
            else:
                since_timestamp = _parse_date(config.since_date)
                until_timestamp = _parse_date(config.until_date, end_of_day=True)

        if use_resume and bulk and mode != SyncMode.full:
            resume_key = None
            full_synced_hint = False

        if mode in (SyncMode.incremental, SyncMode.recent, SyncMode.range):
            full_synced_hint = False

        return SyncPlan(
            since_timestamp=since_timestamp,
            until_timestamp=until_timestamp,
            stop_on_existing=stop_on_existing,
            full_synced_hint=full_synced_hint,
            resume_key=resume_key if mode == SyncMode.full and use_resume and bulk else None,
            complete_key=complete_key,
        )

    async def sync_account(
        self,
        *,
        account: AccountCredential,
        config: SyncConfig,
        bulk: bool,
        use_resume: bool,
        shared_since: int | None,
        shared_until: int | None,
        observer: SyncObserver,
        group_defaults: dict[int, dict[str, Any]] | None = None,
        allow_freq_skip: bool = False,
    ) -> tuple[SyncAccountResult, list[ArticleRecord], SyncSummary | None]:
        mode, recent_days = self._resolve_account_mode(
            account=account,
            config=config,
            group_defaults=group_defaults,
        )
        if config.mode == SyncMode.recent and config.recent_days is None:
            raise ValueError('--recent-days is required for recent mode.')
        if config.mode == SyncMode.range and not config.since_date:
            raise ValueError('--since is required for range mode.')

        if use_resume and bulk and mode == SyncMode.full and config.reset:
            resume_key = f'sync_progress:{account.biz}'
            complete_key = f'sync_complete:{account.biz}'
            with self._storage.transaction():
                self._storage.meta.delete(resume_key)
                self._storage.meta.delete(complete_key)

        if account.is_disabled:
            observer.on_skip('disabled')
            result = SyncAccountResult(
                biz=account.biz,
                nickname=account.nickname,
                saved=0,
                completed=False,
                skipped=True,
                skip_reason='disabled',
                failed=False,
                error=None,
            )
            return result, [], None

        if (
            use_resume
            and bulk
            and mode == SyncMode.full
            and config.skip_minutes is None
            and not config.force
            and self._storage.meta.get(f'sync_complete:{account.biz}') == _today_str()
        ):
            observer.on_skip('completed_today')
            result = SyncAccountResult(
                biz=account.biz,
                nickname=account.nickname,
                saved=0,
                completed=True,
                skipped=True,
                skip_reason='completed_today',
                failed=False,
                error=None,
            )
            return result, [], None

        if not config.force and should_skip_by_time(account.last_synced_at, config.skip_minutes):
            observer.on_skip('recently_synced')
            result = SyncAccountResult(
                biz=account.biz,
                nickname=account.nickname,
                saved=0,
                completed=False,
                skipped=True,
                skip_reason='recently_synced',
                failed=False,
                error=None,
            )
            return result, [], None

        plan = self._build_sync_plan(
            account=account,
            config=config,
            mode=mode,
            recent_days=recent_days,
            shared_since=shared_since,
            shared_until=shared_until,
            use_resume=use_resume,
            bulk=bulk,
        )

        collector = PageCollector() if config.download_content else None
        summary: SyncSummary | None = None
        try:
            summary = await sync_account_core(
                storage=self._storage,
                client=self._client,
                account=account,
                config=config,
                plan=plan,
                login_flow=self._login_flow,
                on_login_required=self._on_login_required,
                collect_existing_ids=bool(config.download_content),
                observer=collector or observer,
            )
        except SyncInterrupted:
            raise
        except RuntimeError as exc:
            message = str(exc)
            if is_freq_control(message) and allow_freq_skip:
                observer.on_skip('freq_control')
                await asyncio.sleep(15)
                result = SyncAccountResult(
                    biz=account.biz,
                    nickname=account.nickname,
                    saved=0,
                    completed=False,
                    skipped=True,
                    skip_reason='freq_control',
                    failed=False,
                    error=None,
                )
                return result, [], None
            if bulk and not is_login_error(message):
                result = SyncAccountResult(
                    biz=account.biz,
                    nickname=account.nickname or account.biz,
                    saved=0,
                    completed=False,
                    skipped=False,
                    skip_reason=None,
                    failed=True,
                    error=message,
                )
                return result, [], None
            raise SyncRunError(message, login_required=is_login_error(message)) from exc

        if summary:
            with self._storage.transaction():
                self._storage.accounts.update_last_synced(account.biz)

        if summary.completed and use_resume and bulk and mode == SyncMode.full and plan.complete_key:
            with self._storage.transaction():
                self._storage.meta.set(plan.complete_key, _today_str())

        result = SyncAccountResult(
            biz=account.biz,
            nickname=account.nickname or account.biz,
            saved=summary.total_saved,
            completed=summary.completed,
            skipped=False,
            skip_reason=None,
            failed=False,
            error=None,
        )
        return result, collector.records if collector else [], summary

    async def sync_accounts(
        self,
        *,
        accounts: list[AccountCredential],
        config: SyncConfig,
        bulk: bool,
        use_resume: bool,
        observer_factory: Callable[[AccountCredential, bool], SyncObserver] | None = None,
        group_defaults: dict[int, dict[str, Any]] | None = None,
        allow_freq_skip: bool = False,
        observer: SyncJobObserver | None = None,
    ) -> SyncReport:
        job_observer = observer or NullSyncJobObserver()
        shared_since, shared_until = self._resolve_shared_window(config)
        accounts_total = len(accounts)
        total_saved = 0
        total_downloaded = 0
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
                downloaded=total_downloaded,
                failed_accounts=failed_accounts,
                accounts_total=accounts_total,
                accounts_done=len(details),
                current_account=current,
            )

        for account in accounts:
            if _get_cancel_event().is_set():
                break
            job_observer.on_account_start(account)
            job_observer.on_account_stage(account, 'listing')
            page_observer = observer_factory(account, bulk) if observer_factory else NullSyncObserver()
            try:
                result, records, summary = await self.sync_account(
                    account=account,
                    config=config,
                    bulk=bulk,
                    use_resume=use_resume,
                    shared_since=shared_since,
                    shared_until=shared_until,
                    observer=page_observer,
                    group_defaults=group_defaults,
                    allow_freq_skip=allow_freq_skip,
                )
            except SyncRunError as exc:
                raise SyncRunError(
                    str(exc),
                    login_required=exc.login_required,
                    report=_build_report(current_account=account),
                ) from exc
            except SyncInterrupted:
                cancelled_result = SyncAccountResult(
                    biz=account.biz,
                    nickname=account.nickname or account.biz,
                    saved=0,
                    completed=False,
                    skipped=False,
                    failed=False,
                    error=None,
                )
                job_observer.on_account_done(cancelled_result, None)
                break
            details.append(result)
            if result.failed:
                failed_accounts += 1
                job_observer.on_account_done(result, summary)
                continue
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

            if config.download_content and self._downloader and records:
                if _get_cancel_event().is_set():
                    raise SyncInterrupted('sync cancelled')
                job_observer.on_account_stage(account, 'content')
                candidates = {item.article_id: item for item in records}
                missing_articles = _select_missing_content(
                    self._storage,
                    account.biz,
                    limit=config.content_limit,
                )
                for missing in missing_articles:
                    candidates.setdefault(missing.article_id, missing)
                if candidates:
                    results, _, _ = await self._downloader.download_many(
                        candidates.values(),
                        with_images=bool(config.download_images),
                        record_images_only=not bool(config.download_images),
                        skip_if_downloaded=True,
                    )
                    total_downloaded += len(results)
            job_observer.on_account_done(result, summary)

        return SyncReport(
            total_saved=total_saved,
            summary=summary_rows,
            details=details,
            downloaded=total_downloaded,
            failed_accounts=failed_accounts,
            accounts_total=accounts_total,
            accounts_done=len(details),
        )


def _build_sync_config(settings: dict[str, Any]) -> SyncConfig:
    return SyncConfig(
        mode=None,
        page_size=DEFAULT_PAGE_SIZE,
        sleep_seconds=float(settings.get('sleep_seconds') or 0),
        reset=False,
        recent_days=None,
        since_date=None,
        until_date=None,
        force=False,
        skip_minutes=settings.get('skip_minutes'),
        download_content=bool(settings.get('download_content')),
        download_images=bool(settings.get('download_images')),
        content_limit=None,
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
        if _should_skip_for_login(storage):
            status = get_sync_status(storage)
            error = status.get('last_error') or 'login_required'
            return SyncJobResult(status=status, report=empty_report, error=error)
        settings = get_sync_settings(storage)
        set_sync_state(storage, status='running', error='', started_at=started_at)
        try:
            storage.sessions.get_login_session()
        except Exception as exc:
            error = str(exc)
            finished_at = utc_now_iso()
            set_sync_state(storage, status='login_required', error=error, finished_at=finished_at)
            with storage.transaction():
                storage.meta.set(SYNC_LOGIN_REQUIRED_AT_KEY, finished_at)
            append_sync_history(
                storage,
                {
                    'started_at': started_at,
                    'finished_at': finished_at,
                    'status': 'login_required',
                    'error': error,
                },
            )
            return SyncJobResult(status=get_sync_status(storage), report=empty_report, error=error)

        error: str | None = None
        report = empty_report
        accounts: list[AccountCredential] = []
        group_defaults: dict[int, dict[str, Any]] = {}
        try:
            accounts = storage.accounts.list_accounts()
            if group_id is not None:
                accounts = [account for account in accounts if account.group_id == group_id]
            if biz_list is not None:
                allowed_biz = set(biz_list)
                accounts = [account for account in accounts if account.biz in allowed_biz]
            job_observer.on_accounts_loaded(accounts)

            group_rows = fetchall_rows(
                storage,
                'SELECT id, sync_mode, sync_recent_days FROM account_groups',
                [],
            )
            for row in group_rows:
                group_defaults[int(row['id'])] = row

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
                        client=app.api_client,
                        downloader=app.downloader,
                    )
                    try:
                        report = await service.sync_accounts(
                            accounts=accounts,
                            config=config,
                            bulk=True,
                            use_resume=True,
                            group_defaults=group_defaults,
                            allow_freq_skip=True,
                            observer_factory=observer_factory,
                            observer=job_observer,
                        )
                    except SyncRunError as exc:
                        error = str(exc)
                        report = exc.report or empty_report
                    except Exception as exc:
                        error = str(exc)
                        report = empty_report
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

        if settings.get('download_images'):
            asyncio.create_task(_run_backfill_images())

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
    'ArticleSyncService',
    'SyncJobResult',
    'SyncRunError',
    'SYNC_RUN_LOCK',
    'run_sync_job',
]
