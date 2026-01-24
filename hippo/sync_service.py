"""Sync scheduling and settings helpers."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from .downloader import ArticleDownloader
from .emailer import get_email_settings, send_email
from .http import MPClient
from .models import AccountCredential, ArticleRecord
from .storage import PostgresStorage, open_storage
from .sync_core import is_freq_control, is_login_error, sync_account_core
from .utils import fetchall_rows, load_meta_json, parse_iso_date_to_timestamp, save_meta_json, should_skip_by_time

SYNC_STATUS_KEY = 'sync:last_status'
SYNC_ERROR_KEY = 'sync:last_error'
SYNC_STARTED_KEY = 'sync:last_started_at'
SYNC_FINISHED_KEY = 'sync:last_finished_at'
SYNC_HISTORY_KEY = 'sync:history'
SYNC_SETTINGS_KEY = 'sync:settings'
ALERT_SENT_KEY = 'sync:alert_sent'

_SYNC_MODES = {'incremental', 'recent', 'full', 'range'}
_logger = logging.getLogger('hippo.sync')


def default_sync_settings() -> dict[str, Any]:
    return {
        'enabled': False,
        'interval_minutes': 60,
        'mode': 'incremental',
        'recent_days': 7,
        'page_size': 10,
        'page_limit': 2,
        'sleep_seconds': 0.05,
        'download_content': True,
        'download_images': True,
        'content_limit': 20,
        'skip_minutes': 30,
        'alert_enabled': False,
        'alert_email': '',
    }


def get_sync_settings(storage: PostgresStorage) -> dict[str, Any]:
    settings = load_meta_json(storage, SYNC_SETTINGS_KEY, default_sync_settings())
    defaults = default_sync_settings()
    return {**defaults, **(settings or {})}


def set_sync_settings(storage: PostgresStorage, updates: dict[str, Any]) -> dict[str, Any]:
    current = get_sync_settings(storage)
    current.update(updates)
    save_meta_json(storage, SYNC_SETTINGS_KEY, current)
    return current


def append_sync_history(storage: PostgresStorage, entry: dict[str, Any]) -> None:
    history = load_meta_json(storage, SYNC_HISTORY_KEY, [])
    if not isinstance(history, list):
        history = []
    history.insert(0, entry)
    history = history[:50]
    save_meta_json(storage, SYNC_HISTORY_KEY, history)


def get_sync_status(storage: PostgresStorage) -> dict[str, Any]:
    return {
        'status': storage.get_meta(SYNC_STATUS_KEY) or 'idle',
        'last_started_at': storage.get_meta(SYNC_STARTED_KEY),
        'last_finished_at': storage.get_meta(SYNC_FINISHED_KEY),
        'last_error': storage.get_meta(SYNC_ERROR_KEY),
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
    if status is not None:
        storage.set_meta(SYNC_STATUS_KEY, status)
    if error is not None:
        storage.set_meta(SYNC_ERROR_KEY, error)
    if started_at is not None:
        storage.set_meta(SYNC_STARTED_KEY, started_at)
    if finished_at is not None:
        storage.set_meta(SYNC_FINISHED_KEY, finished_at)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    limit: int,
) -> list[ArticleRecord]:
    if limit <= 0:
        return []
    articles = storage.list_articles(biz, limit=limit)
    get_content_ids = getattr(storage, 'get_article_content_ids', None)
    if callable(get_content_ids):
        try:
            ids = get_content_ids(biz, [article.article_id for article in articles])
            return [article for article in articles if article.article_id not in ids]
        except Exception:
            return articles
    return articles


async def _sync_account_articles(
    *,
    storage: PostgresStorage,
    client: MPClient,
    downloader: ArticleDownloader,
    account: AccountCredential,
    settings: dict[str, Any],
    group_defaults: dict[int, dict[str, Any]] | None = None,
) -> tuple[int, int]:
    page_size = max(int(settings.get('page_size') or 10), 1)
    page_limit = settings.get('page_limit')
    if page_limit is not None:
        page_limit = max(int(page_limit), 1)
    group_sync = None
    if group_defaults and account.group_id is not None:
        group_sync = group_defaults.get(account.group_id)
    group_mode = None
    group_recent_days = None
    if group_sync:
        group_mode = group_sync.get('sync_mode')
        group_recent_days = group_sync.get('sync_recent_days')
    mode = (account.sync_mode or group_mode or settings.get('mode') or 'incremental').strip().lower()
    if mode not in _SYNC_MODES:
        mode = 'incremental'
    recent_days = account.sync_recent_days
    if recent_days is None:
        recent_days = group_recent_days if group_recent_days is not None else settings.get('recent_days')
    now = datetime.now(timezone.utc)
    since_ts: int | None = None
    until_ts: int | None = None
    stop_on_existing = False
    if mode == 'incremental':
        stop_on_existing = True
        if account.last_synced_at:
            since_ts = int(account.last_synced_at.timestamp())
    elif mode == 'recent':
        recent_days = max(int(recent_days or 1), 1)
        since_ts = int((now.timestamp() - recent_days * 86400))
    elif mode == 'range':
        since_ts = _parse_date(settings.get('since'))
        until_ts = _parse_date(settings.get('until'), end_of_day=True)

    total_saved = 0
    to_download: list[ArticleRecord] = []
    async for event, payload in sync_account_core(
        storage=storage,
        client=client,
        account=account,
        page_size=page_size,
        pages=page_limit,
        sleep_seconds=float(settings.get('sleep_seconds') or 0),
        since_timestamp=since_ts,
        until_timestamp=until_ts,
        stop_on_existing=stop_on_existing,
        collect_existing_ids=True,
    ):
        if event == 'page':
            records = payload.get('records') or []
            existing_ids = payload.get('existing_ids') or set()
            if records:
                new_records = [r for r in records if r.article_id not in existing_ids]
                to_download.extend(new_records)
        elif event == 'complete':
            total_saved = int(payload.get('total_saved', 0))

    downloaded = 0
    if settings.get('download_content'):
        content_limit = int(settings.get('content_limit') or 0)
        candidates = {item.article_id: item for item in to_download}
        for missing in _select_missing_content(storage, account.biz, limit=content_limit):
            candidates.setdefault(missing.article_id, missing)
        if candidates:
            results, _, _ = await downloader.download_many(
                candidates.values(),
                with_images=bool(settings.get('download_images')),
                record_images_only=not bool(settings.get('download_images')),
                skip_if_downloaded=True,
            )
            downloaded = len(results)
    return total_saved, downloaded


class SyncScheduler:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._trigger = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._trigger.set()
        if self._thread:
            self._thread.join(timeout=5)

    def trigger(self) -> None:
        self._trigger.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            with open_storage() as storage:
                settings = get_sync_settings(storage)
            if not settings.get('enabled'):
                self._trigger.wait(timeout=10)
                self._trigger.clear()
                continue
            interval = max(int(settings.get('interval_minutes') or 1), 1) * 60
            self._trigger.wait(timeout=interval)
            self._trigger.clear()
            if self._stop.is_set():
                break
            self.run_once()

    def run_once(self) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {'status': 'running'}
        try:
            return self._run_sync()
        finally:
            self._lock.release()

    def run_group(self, group_id: int) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {'status': 'running'}
        try:
            return self._run_sync(group_id=group_id)
        finally:
            self._lock.release()

    def _run_sync(self, *, group_id: int | None = None) -> dict[str, Any]:
        return asyncio.run(self._run_sync_async(group_id=group_id))

    async def _run_sync_async(self, *, group_id: int | None = None) -> dict[str, Any]:
        started_at = _utc_now_iso()
        with open_storage() as storage:
            settings = get_sync_settings(storage)
            set_sync_state(storage, status='running', error='', started_at=started_at)
            try:
                storage.get_login_session()
            except Exception as exc:
                error = str(exc)
                set_sync_state(storage, status='login_required', error=error, finished_at=_utc_now_iso())
                append_sync_history(
                    storage,
                    {
                        'started_at': started_at,
                        'finished_at': _utc_now_iso(),
                        'status': 'login_required',
                        'error': error,
                    },
                )
                return get_sync_status(storage)

            accounts = storage.list_accounts()
            if group_id is not None:
                accounts = [account for account in accounts if account.group_id == group_id]
            group_defaults: dict[int, dict[str, Any]] = {}
            group_rows = fetchall_rows(
                storage,
                'SELECT id, sync_mode, sync_recent_days FROM account_groups',
                [],
            )
            for row in group_rows:
                group_defaults[int(row['id'])] = row
            total_saved = 0
            total_downloaded = 0
            skipped_accounts = 0
            error: str | None = None
            async with MPClient() as client:
                async with ArticleDownloader(
                    client=client,
                    storage=storage,
                    enable_image_worker=bool(settings.get('download_images')),
                ) as downloader:
                    for account in accounts:
                        if account.is_disabled:
                            skipped_accounts += 1
                            continue
                        if should_skip_by_time(account.last_synced_at, settings.get('skip_minutes')):
                            skipped_accounts += 1
                            continue
                        try:
                            saved, downloaded = await _sync_account_articles(
                                storage=storage,
                                client=client,
                                downloader=downloader,
                                account=account,
                                settings=settings,
                                group_defaults=group_defaults,
                            )
                        except Exception as exc:
                            message = str(exc)
                            if is_login_error(message):
                                error = message
                                break
                            if is_freq_control(message):
                                await asyncio.sleep(15)
                                continue
                            error = message
                            break
                        total_saved += saved
                        total_downloaded += downloaded
                    if settings.get('download_images'):
                        await downloader.wait_for_images()

            finished_at = _utc_now_iso()
            if error:
                status = 'login_required' if is_login_error(error) else 'failed'
                set_sync_state(storage, status=status, error=error, finished_at=finished_at)
            else:
                set_sync_state(storage, status='success', error='', finished_at=finished_at)
                storage.delete_meta(ALERT_SENT_KEY)
            append_sync_history(
                storage,
                {
                    'started_at': started_at,
                    'finished_at': finished_at,
                    'status': 'login_required' if error and is_login_error(error) else ('failed' if error else 'success'),
                    'error': error or '',
                    'saved': total_saved,
                    'downloaded': total_downloaded,
                    'skipped_accounts': skipped_accounts,
                },
            )
            if error and not storage.get_meta(ALERT_SENT_KEY):
                sync_settings = get_sync_settings(storage)
                if sync_settings.get('alert_enabled') and sync_settings.get('alert_email'):
                    subject = 'Hippo sync failed'
                    body = f'Status: {status}\\nError: {error}\\nStarted: {started_at}\\nFinished: {finished_at}'
                    try:
                        email_settings = get_email_settings(storage)
                        send_email(email_settings, to_email=str(sync_settings.get('alert_email')), subject=subject, body=body)
                        storage.set_meta(ALERT_SENT_KEY, '1')
                    except Exception as exc:
                        _logger.warning('Failed to send alert email: %s', exc)
            return get_sync_status(storage)


__all__ = [
    'SyncScheduler',
    'append_sync_history',
    'default_sync_settings',
    'get_sync_settings',
    'get_sync_status',
    'set_sync_settings',
    'set_sync_state',
]
