"""Sync scheduling and settings helpers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .container import build_sync_container
from .emailer import get_email_settings, send_email
from .file_storage import FileStorageError
from .wechat_api import WeChatApiClient
from .models import AccountCredential, ArticleRecord
from .storage import PostgresStorage, open_storage
from .sync_core import is_freq_control, is_login_error, sync_account_core

# Optional background image backfill after scheduled sync
async def _run_backfill_images() -> None:
    try:
        from .cli import _backfill_article_images_async
    except Exception:
        return
    await _backfill_article_images_async(
        pg_dsn=None,
        limit=None,
        workers=4,
        retries=3,
        sleep_base=0.5,
        retry_failed=False,
        dry_run=False,
    )
from .sync_types import (
    NullSyncObserver,
    SyncAccountResult,
    SyncConfig,
    SyncMode,
    SyncObserver,
    SyncPlan,
    SyncReport,
    SyncSummary,
)
from .utils import (
    fetchall_rows,
    load_meta_json,
    parse_iso_date_to_timestamp,
    save_meta_json,
    should_skip_by_time,
)

SYNC_STATUS_KEY = 'sync:last_status'
SYNC_ERROR_KEY = 'sync:last_error'
SYNC_STARTED_KEY = 'sync:last_started_at'
SYNC_FINISHED_KEY = 'sync:last_finished_at'
SYNC_HISTORY_KEY = 'sync:history'
SYNC_SETTINGS_KEY = 'sync:settings'
ALERT_SENT_KEY = 'sync:alert_sent'
SYNC_LOGIN_REQUIRED_AT_KEY = 'sync:login_required_at'

_logger = logging.getLogger('hippo.sync')
_DEFAULT_RECENT_DAYS = 7
_DEFAULT_PAGE_SIZE = 10
_DEFAULT_WINDOW_START_HOUR = 6
_DEFAULT_WINDOW_END_HOUR = 24
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
        'window_start_hour': _DEFAULT_WINDOW_START_HOUR,
        'window_end_hour': _DEFAULT_WINDOW_END_HOUR,
        'sleep_seconds': 0.05,
        'download_content': True,
        'download_images': True,
        'skip_minutes': 30,
        'alert_enabled': False,
        'alert_email': '',
    }


def _normalize_window_start_hour(value: Any) -> int:
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_WINDOW_START_HOUR
    return min(max(hour, 0), 23)


def _normalize_window_end_hour(value: Any) -> int:
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_WINDOW_END_HOUR
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


def _build_sync_config(settings: dict[str, Any]) -> SyncConfig:
    return SyncConfig(
        mode=None,
        page_size=_DEFAULT_PAGE_SIZE,
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


def get_sync_settings(storage: PostgresStorage) -> dict[str, Any]:
    settings = load_meta_json(storage, SYNC_SETTINGS_KEY, default_sync_settings())
    defaults = default_sync_settings()
    merged = {**defaults, **(settings or {})}
    start_hour, end_hour = _get_window_hours(merged)
    merged['window_start_hour'] = start_hour
    merged['window_end_hour'] = end_hour
    # Remove legacy fields that are no longer supported by the settings UI.
    for key in ('mode', 'recent_days', 'page_size', 'content_limit', 'since', 'until'):
        merged.pop(key, None)
    return merged


def set_sync_settings(storage: PostgresStorage, updates: dict[str, Any]) -> dict[str, Any]:
    current = get_sync_settings(storage)
    current.update(updates)
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
) -> None:
    if not error or storage.meta.get(ALERT_SENT_KEY):
        return
    sync_settings = get_sync_settings(storage)
    if not sync_settings.get('alert_enabled') or not sync_settings.get('alert_email'):
        return
    subject = 'Hippo sync failed'
    body = f'Status: {status}\nError: {error}\nStarted: {started_at}\nFinished: {finished_at}'
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_utc_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _to_utc_dt(parsed)


def _get_login_updated_at(storage: PostgresStorage) -> datetime | None:
    updated_at = storage.sessions.get_login_updated_at()
    if not updated_at:
        return None
    return _to_utc_dt(updated_at)


def _should_skip_for_login(storage: PostgresStorage) -> bool:
    if storage.meta.get(SYNC_STATUS_KEY) != 'login_required':
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
    except Exception:
        return articles
    return articles


def _to_utc_timestamp(value: datetime | None) -> int | None:
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return int(value.timestamp())


def _today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class SyncRunError(RuntimeError):
    def __init__(self, message: str, *, login_required: bool = False) -> None:
        super().__init__(message)
        self.login_required = login_required


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
            since = int((datetime.now(timezone.utc) - timedelta(days=config.recent_days)).timestamp())
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
            recent_days = group_recent_days if group_recent_days is not None else _DEFAULT_RECENT_DAYS
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
                since_timestamp = int((datetime.now(timezone.utc).timestamp() - days * 86400))
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
        on_account_start: Callable[[AccountCredential], None] | None = None,
        on_account_done: Callable[[SyncAccountResult, SyncSummary | None], None] | None = None,
        on_account_stage: Callable[[AccountCredential, str], None] | None = None,
    ) -> SyncReport:
        shared_since, shared_until = self._resolve_shared_window(config)
        total_saved = 0
        total_downloaded = 0
        summary_rows: list[tuple[str, int]] = []
        details: list[SyncAccountResult] = []

        for account in accounts:
            if on_account_start:
                on_account_start(account)
            if on_account_stage:
                on_account_stage(account, 'listing')
            observer = observer_factory(account, bulk) if observer_factory else NullSyncObserver()
            result, records, summary = await self.sync_account(
                account=account,
                config=config,
                bulk=bulk,
                use_resume=use_resume,
                shared_since=shared_since,
                shared_until=shared_until,
                observer=observer,
                group_defaults=group_defaults,
                allow_freq_skip=allow_freq_skip,
            )
            details.append(result)
            if result.skipped and not bulk:
                if on_account_done:
                    on_account_done(result, summary)
                return SyncReport(total_saved=0, summary=[], details=details, downloaded=0)
            if result.skipped:
                if on_account_done:
                    on_account_done(result, summary)
                continue

            if summary:
                total_saved += summary.total_saved
                summary_rows.append((result.nickname or result.biz, summary.total_saved))

            if config.download_content and self._downloader and records:
                if on_account_stage:
                    on_account_stage(account, 'content')
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
            if on_account_done:
                on_account_done(result, summary)

        return SyncReport(
            total_saved=total_saved,
            summary=summary_rows,
            details=details,
            downloaded=total_downloaded,
        )


def _build_sync_config(settings: dict[str, Any]) -> SyncConfig:
    return SyncConfig(
        mode=None,
        page_size=_DEFAULT_PAGE_SIZE,
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
    on_account_start: Callable[[AccountCredential], None] | None = None,
    on_account_done: Callable[[SyncAccountResult, SyncSummary | None], None] | None = None,
    on_accounts_loaded: Callable[[list[AccountCredential]], None] | None = None,
    on_account_stage: Callable[[AccountCredential, str], None] | None = None,
    lock: asyncio.Lock | None = None,
    on_lock_acquired: Callable[[], None] | None = None,
    on_images_start: Callable[[], None] | None = None,
    on_images_done: Callable[[], None] | None = None,
) -> SyncJobResult:
    if lock:
        async with lock:
            if on_lock_acquired:
                on_lock_acquired()
            return await run_sync_job(
                group_id=group_id,
                biz_list=biz_list,
                observer_factory=observer_factory,
                on_account_start=on_account_start,
                on_account_done=on_account_done,
                on_accounts_loaded=on_accounts_loaded,
                on_account_stage=on_account_stage,
                lock=None,
                on_lock_acquired=None,
                on_images_start=on_images_start,
                on_images_done=on_images_done,
            )
    if on_lock_acquired:
        on_lock_acquired()
    started_at = _utc_now_iso()
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
            finished_at = _utc_now_iso()
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

        accounts = storage.accounts.list_accounts()
        if group_id is not None:
            accounts = [account for account in accounts if account.group_id == group_id]
        if biz_list is not None:
            allowed_biz = set(biz_list)
            accounts = [account for account in accounts if account.biz in allowed_biz]
        if on_accounts_loaded:
            on_accounts_loaded(accounts)
        group_defaults: dict[int, dict[str, Any]] = {}
        group_rows = fetchall_rows(
            storage,
            'SELECT id, sync_mode, sync_recent_days FROM account_groups',
            [],
        )
        for row in group_rows:
            group_defaults[int(row['id'])] = row

        error: str | None = None
        report = empty_report
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
                        on_account_start=on_account_start,
                        on_account_done=on_account_done,
                        on_account_stage=on_account_stage,
                    )
                except SyncRunError as exc:
                    error = str(exc)
                    report = empty_report
                except Exception as exc:
                    error = str(exc)
                    report = empty_report
                if settings.get('download_images') and app.downloader:
                    if on_images_start:
                        on_images_start()
                    await app.downloader.wait_for_images()
                    if on_images_done:
                        on_images_done()

        # Fire-and-forget image backfill to ensure missing images are picked up
        if settings.get('download_images'):
            try:
                asyncio.create_task(_run_backfill_images())
            except RuntimeError:
                loop = asyncio.get_event_loop()
                loop.create_task(_run_backfill_images())

        finished_at = _utc_now_iso()
        if error:
            status = 'login_required' if is_login_error(error) else 'failed'
            set_sync_state(storage, status=status, error=error, finished_at=finished_at)
            if status == 'login_required':
                with storage.transaction():
                    storage.meta.set(SYNC_LOGIN_REQUIRED_AT_KEY, finished_at)
        else:
            status = 'success'
            set_sync_state(storage, status='success', error='', finished_at=finished_at)
            with storage.transaction():
                storage.meta.delete(ALERT_SENT_KEY)
                storage.meta.delete(SYNC_LOGIN_REQUIRED_AT_KEY)
        skipped_accounts = sum(
            1
            for item in report.details
            if item.skipped and item.skip_reason in ('disabled', 'recently_synced')
        )
        append_sync_history(
            storage,
            {
                'started_at': started_at,
                'finished_at': finished_at,
                'status': 'login_required' if error and is_login_error(error) else ('failed' if error else 'success'),
                'error': error or '',
                'saved': report.total_saved,
                'downloaded': report.downloaded,
                'skipped_accounts': skipped_accounts,
            },
        )
        _send_sync_alert(
            storage,
            status=status,
            error=error or '',
            started_at=started_at,
            finished_at=finished_at,
        )
        return SyncJobResult(status=get_sync_status(storage), report=report, error=error)


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
        try:
            await asyncio.wait_for(self._trigger.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
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
        if self._lock.locked():
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
