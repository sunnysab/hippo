"""Sync scheduling and settings helpers."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import DEFAULT_PAGE_SIZE, DEFAULT_RECENT_DAYS, DEFAULT_WINDOW_END_HOUR, DEFAULT_WINDOW_START_HOUR
from .container import build_sync_container
from .downloader import ArticleDownloader
from .emailer import get_email_settings, send_email
from .file_storage import FileStorageError
from .models import AccountCredential, ArticleRecord
from .storage import PostgresStorage, open_storage
from .sync_core import (
    SyncInterrupted,
    _get_cancel_event,
    is_freq_control,
    is_login_error,
    reset_sync_cancel,
    sync_account_core,
)
from .utils import to_utc_dt
from .wechat_api import WeChatApiClient


# Optional background image backfill after scheduled sync
async def _run_backfill_images() -> None:
    try:
        from .image_backfill import backfill_article_images
    except Exception:
        return
    try:
        await backfill_article_images()
    except Exception:
        _logger.exception('Background image backfill failed.')


import contextlib

from .storage import fetchall_rows, load_meta_json, save_meta_json
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

SYNC_STATUS_KEY = 'sync:last_status'
SYNC_ERROR_KEY = 'sync:last_error'
SYNC_STARTED_KEY = 'sync:last_started_at'
SYNC_FINISHED_KEY = 'sync:last_finished_at'
SYNC_HISTORY_KEY = 'sync:history'
SYNC_SETTINGS_KEY = 'sync:settings'
ALERT_SENT_KEY = 'sync:alert_sent'
SYNC_LOGIN_REQUIRED_AT_KEY = 'sync:login_required_at'

_logger = logging.getLogger('hippo.sync')

_ARTICLE_EXCLUDE_KEYWORD_LIMIT = 20
SYNC_RUN_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class SyncJobResult:
    status: dict[str, Any]
    report: SyncReport
    error: str | None


def default_sync_settings() -> dict[str, Any]:
    return {
        'enabled': False,
        'interval_minutes': 60,
        'window_start_hour': DEFAULT_WINDOW_START_HOUR,
        'window_end_hour': DEFAULT_WINDOW_END_HOUR,
        'sleep_seconds': 0.05,
        'download_content': True,
        'download_images': True,
        'skip_minutes': 30,
        'article_exclude_keywords': '',
        'alert_enabled': False,
        'alert_email': '',
    }


def _split_article_exclude_keywords(value: Any) -> list[str]:
    if value in (None, ''):
        return []
    keywords: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r'[,;\n]+', str(value)):
        term = chunk.strip()
        if not term:
            continue
        dedupe_key = term.lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        keywords.append(term)
        if len(keywords) >= _ARTICLE_EXCLUDE_KEYWORD_LIMIT:
            break
    return keywords


def _normalize_article_exclude_keywords(value: Any) -> str:
    return '\n'.join(_split_article_exclude_keywords(value))


def _normalize_window_start_hour(value: Any) -> int:
    try:
        hour = int(value)
    except TypeError, ValueError:
        return DEFAULT_WINDOW_START_HOUR
    return min(max(hour, 0), 23)


def _normalize_window_end_hour(value: Any) -> int:
    try:
        hour = int(value)
    except TypeError, ValueError:
        return DEFAULT_WINDOW_END_HOUR
    return min(max(hour, 0), 24)


def _get_window_hours(settings: dict[str, Any]) -> tuple[int, int]:
    return (
        _normalize_window_start_hour(settings.get('window_start_hour')),
        _normalize_window_end_hour(settings.get('window_end_hour')),
    )


def _is_within_sync_window(now: datetime, *, start_hour: int, end_hour: int) -> bool:
    current_minute = now.hour * 60 + now.minute
    start_minute = (start_hour % 24) * 60
    end_minute = 24 * 60 if end_hour == 24 else (end_hour % 24) * 60
    if start_minute == end_minute:
        return True
    if start_minute < end_minute:
        return start_minute <= current_minute < end_minute
    return current_minute >= start_minute or current_minute < end_minute


def _seconds_until_window_start(now: datetime, *, start_hour: int, end_hour: int) -> float:
    if _is_within_sync_window(now, start_hour=start_hour, end_hour=end_hour):
        return 0.0
    target_hour = start_hour % 24
    target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max((target - now).total_seconds(), 1.0)


def get_sync_settings(storage: PostgresStorage) -> dict[str, Any]:
    settings = load_meta_json(storage, SYNC_SETTINGS_KEY, default_sync_settings())
    defaults = default_sync_settings()
    merged = {**defaults, **(settings or {})}
    start_hour, end_hour = _get_window_hours(merged)
    merged['window_start_hour'] = start_hour
    merged['window_end_hour'] = end_hour
    merged['article_exclude_keywords'] = _normalize_article_exclude_keywords(
        merged.get('article_exclude_keywords'),
    )
    return merged


def set_sync_settings(storage: PostgresStorage, updates: dict[str, Any]) -> dict[str, Any]:
    current = get_sync_settings(storage)
    current.update(updates)
    if 'article_exclude_keywords' in current:
        current['article_exclude_keywords'] = _normalize_article_exclude_keywords(
            current.get('article_exclude_keywords'),
        )
    with storage.transaction():
        save_meta_json(storage, SYNC_SETTINGS_KEY, current)
    return current


def append_sync_history(storage: PostgresStorage, entry: dict[str, Any]) -> None:
    history = load_meta_json(storage, SYNC_HISTORY_KEY, [])
    if not isinstance(history, list):
        history = []
    history.insert(0, entry)
    history = history[:50]
    with storage.transaction():
        save_meta_json(storage, SYNC_HISTORY_KEY, history)


def _send_sync_alert(
    storage: PostgresStorage,
    *,
    status: str,
    error: str,
    started_at: str,
    finished_at: str,
    report: SyncReport | None = None,
) -> None:
    if not error or storage.meta.get(ALERT_SENT_KEY):
        return
    sync_settings = get_sync_settings(storage)
    if not sync_settings.get('alert_enabled') or not sync_settings.get('alert_email'):
        return
    subject = 'Hippo sync failed'
    lines = [
        f'Status: {status}',
        f'Error: {error}',
        f'Started: {started_at}',
        f'Finished: {finished_at}',
    ]
    if report is not None:
        lines.append(f'Saved: {report.total_saved}')
        lines.append(f'Downloaded: {report.downloaded}')
        if report.accounts_total > 0:
            lines.append(f'Progress: {report.accounts_done}/{report.accounts_total}')
        current_account = report.current_account or {}
        current_nickname = str(current_account.get('nickname') or '').strip()
        current_biz = str(current_account.get('biz') or '').strip()
        if current_nickname or current_biz:
            lines.append(f'Current account: {current_nickname or current_biz}')
    body = '\n'.join(lines)
    try:
        email_settings = get_email_settings(storage)
        send_email(email_settings, to_email=str(sync_settings.get('alert_email')), subject=subject, body=body)
        with storage.transaction():
            storage.meta.set(ALERT_SENT_KEY, '1')
    except Exception as exc:
        _logger.warning('Failed to send alert email: %s', exc)


def get_sync_status(storage: PostgresStorage) -> dict[str, Any]:
    return {
        'status': storage.meta.get(SYNC_STATUS_KEY) or 'idle',
        'last_started_at': storage.meta.get(SYNC_STARTED_KEY),
        'last_finished_at': storage.meta.get(SYNC_FINISHED_KEY),
        'last_error': storage.meta.get(SYNC_ERROR_KEY),
        'history': load_meta_json(storage, SYNC_HISTORY_KEY, []),
    }


def set_sync_state(
    storage: PostgresStorage,
    *,
    status: str | None = None,
    error: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> None:
    with storage.transaction():
        if status is not None:
            storage.meta.set(SYNC_STATUS_KEY, status)
        if error is not None:
            storage.meta.set(SYNC_ERROR_KEY, error)
        if started_at is not None:
            storage.meta.set(SYNC_STARTED_KEY, started_at)
        if finished_at is not None:
            storage.meta.set(SYNC_FINISHED_KEY, finished_at)


def _persist_sync_outcome(
    storage: PostgresStorage,
    *,
    started_at: str,
    finished_at: str,
    error: str | None,
    report: SyncReport,
) -> dict[str, Any]:
    if error:
        cancelled = error == 'Cancelled by user'
        if is_login_error(error):
            status = 'login_required'
        elif cancelled:
            status = 'cancelled'
        else:
            status = 'failed'
        set_sync_state(storage, status=status, error='' if cancelled else error, finished_at=finished_at)
        if status == 'login_required':
            with storage.transaction():
                storage.meta.set(SYNC_LOGIN_REQUIRED_AT_KEY, finished_at)
    else:
        status = 'success'
        cancelled = False
        set_sync_state(storage, status='success', error='', finished_at=finished_at)
        with storage.transaction():
            storage.meta.delete(ALERT_SENT_KEY)
            storage.meta.delete(SYNC_LOGIN_REQUIRED_AT_KEY)

    skipped_accounts = sum(
        1 for item in report.details if item.skipped and item.skip_reason in ('disabled', 'recently_synced')
    )
    failed_accounts = sum(1 for item in report.details if item.failed)
    append_sync_history(
        storage,
        {
            'started_at': started_at,
            'finished_at': finished_at,
            'status': status,
            'error': '' if cancelled else (error or ''),
            'saved': report.total_saved,
            'downloaded': report.downloaded,
            'skipped_accounts': skipped_accounts,
            'failed_accounts': failed_accounts,
            'accounts_total': report.accounts_total,
            'accounts_done': report.accounts_done,
            'current_account': report.current_account,
        },
    )
    if not cancelled:
        _send_sync_alert(
            storage,
            status=status,
            error=error or '',
            started_at=started_at,
            finished_at=finished_at,
            report=report,
        )
    return get_sync_status(storage)


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return to_utc_dt(parsed)


def _get_login_updated_at(storage: PostgresStorage) -> datetime | None:
    updated_at = storage.sessions.get_login_updated_at()
    if not updated_at:
        return None
    return to_utc_dt(updated_at)


def _should_skip_for_login(storage: PostgresStorage) -> bool:
    if storage.meta.get(SYNC_STATUS_KEY) != 'login_required':
        return False
    last_error = storage.meta.get(SYNC_ERROR_KEY) or ''
    if last_error and not is_login_error(last_error):
        with storage.transaction():
            storage.meta.delete(SYNC_LOGIN_REQUIRED_AT_KEY)
            storage.meta.delete(ALERT_SENT_KEY)
            storage.meta.delete(SYNC_ERROR_KEY)
            storage.meta.set(SYNC_STATUS_KEY, 'failed')
        return False
    blocked_at = _parse_iso_datetime(storage.meta.get(SYNC_LOGIN_REQUIRED_AT_KEY))
    if not blocked_at:
        return False
    last_login = _get_login_updated_at(storage)
    if not last_login or last_login <= blocked_at:
        return True
    with storage.transaction():
        storage.meta.delete(SYNC_LOGIN_REQUIRED_AT_KEY)
        storage.meta.delete(ALERT_SENT_KEY)
        storage.meta.set(SYNC_STATUS_KEY, 'idle')
    return False


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
        _logger.warning('Failed to query article content IDs (biz=%s): %s', biz, exc)
        return articles


def _to_utc_timestamp(value: datetime | None) -> int | None:
    if not value:
        return None
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return int(value.timestamp())


def _today_str() -> str:
    return datetime.now(UTC).date().isoformat()


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
            # Mark account as synced even when no new articles were saved, so subsequent
            # runs within skip window can be skipped correctly.
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
            _send_sync_alert(
                storage,
                status='login_required',
                error=error,
                started_at=started_at,
                finished_at=finished_at,
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
            _logger.exception('Sync job failed unexpectedly')
            error = str(exc)
            report = empty_report

        # Fire-and-forget image backfill to ensure missing images are picked up
        if settings.get('download_images'):
            try:
                asyncio.create_task(_run_backfill_images())
            except RuntimeError:
                loop = asyncio.get_event_loop()
                loop.create_task(_run_backfill_images())

        try:
            storage.rollback()
        except Exception as exc:
            _logger.warning('Failed to rollback storage connection: %s', exc)

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
            _logger.exception('Failed to persist sync outcome; retrying with a fresh connection.')
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
                _logger.exception('Failed to persist sync outcome with a fresh connection.')
                return SyncJobResult(status=get_sync_status(storage), report=report, error=error or 'failed')


class SyncScheduler:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._trigger = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._trigger.clear()
        self._loop = asyncio.get_running_loop()
        self._task = self._loop.create_task(self._loop_sync())

    async def stop(self) -> None:
        self._stop.set()
        self._trigger.set()
        if self._task:
            await self._task
        self._task = None

    def trigger(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._trigger.set)
        else:
            self._trigger.set()

    async def _wait(self, timeout: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._trigger.wait(), timeout=timeout)
        self._trigger.clear()

    async def _loop_sync(self) -> None:
        last_run_duration = 0.0
        while not self._stop.is_set():
            with open_storage() as storage:
                settings = get_sync_settings(storage)
            if not settings.get('enabled'):
                last_run_duration = 0.0
                await self._wait(10)
                continue
            start_hour, end_hour = _get_window_hours(settings)
            now = datetime.now()
            if not _is_within_sync_window(now, start_hour=start_hour, end_hour=end_hour):
                last_run_duration = 0.0
                await self._wait(_seconds_until_window_start(now, start_hour=start_hour, end_hour=end_hour))
                continue
            interval = max(int(settings.get('interval_minutes') or 1), 1) * 60
            wait_seconds = max(interval - last_run_duration, 0)
            await self._wait(wait_seconds)
            if self._stop.is_set():
                break
            now = datetime.now()
            if not _is_within_sync_window(now, start_hour=start_hour, end_hour=end_hour):
                last_run_duration = 0.0
                continue
            loop = self._loop or asyncio.get_running_loop()
            started_at = loop.time()
            await self.run_once()
            last_run_duration = max(loop.time() - started_at, 0.0)

    async def run_once(self, *, group_id: int | None = None) -> dict[str, Any]:
        if self._lock.locked() or SYNC_RUN_LOCK.locked():
            return {'status': 'running'}
        async with self._lock:
            return await self._run_sync_async(group_id=group_id)

    async def run_group(self, group_id: int) -> dict[str, Any]:
        return await self.run_once(group_id=group_id)

    async def _run_sync_async(self, *, group_id: int | None = None) -> dict[str, Any]:
        result = await run_sync_job(group_id=group_id, lock=SYNC_RUN_LOCK)
        return result.status


__all__ = [
    'ArticleSyncService',
    'SyncJobResult',
    'SyncRunError',
    'SyncScheduler',
    'append_sync_history',
    'default_sync_settings',
    'get_sync_settings',
    'get_sync_status',
    'run_sync_job',
    'set_sync_settings',
    'set_sync_state',
]
