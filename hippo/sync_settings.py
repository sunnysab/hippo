"""Sync settings, status, history, and alert management."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import DEFAULT_WINDOW_END_HOUR, DEFAULT_WINDOW_START_HOUR
from .emailer import get_email_settings, send_email
from .storage import PostgresStorage, load_meta_json, save_meta_json
from .sync_core import is_login_error
from .sync_types import SyncReport
from .utils import to_utc_dt

SYNC_STATUS_KEY = 'sync:last_status'
SYNC_ERROR_KEY = 'sync:last_error'
SYNC_STARTED_KEY = 'sync:last_started_at'
SYNC_FINISHED_KEY = 'sync:last_finished_at'
SYNC_HISTORY_KEY = 'sync:history'
SYNC_SETTINGS_KEY = 'sync:settings'
ALERT_SENT_KEY = 'sync:alert_sent'
SYNC_LOGIN_REQUIRED_AT_KEY = 'sync:login_required_at'

_ARTICLE_EXCLUDE_KEYWORD_LIMIT = 20

_logger = logging.getLogger('hippo.sync')


def default_sync_settings() -> dict[str, Any]:
    return {
        'enabled': False,
        'interval_minutes': 60,
        'window_start_hour': DEFAULT_WINDOW_START_HOUR,
        'window_end_hour': DEFAULT_WINDOW_END_HOUR,
        'sleep_seconds': 3.0,
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
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_START_HOUR
    return min(max(hour, 0), 23)


def _normalize_window_end_hour(value: Any) -> int:
    try:
        hour = int(value)
    except (TypeError, ValueError):
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
        storage.meta.delete(SYNC_ERROR_KEY)
        storage.meta.set(SYNC_STATUS_KEY, 'idle')
    return False


def _to_utc_timestamp(value: datetime | None) -> int | None:
    if not value:
        return None
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return int(value.timestamp())


def _today_str() -> str:
    return datetime.now(UTC).date().isoformat()


__all__ = [
    'ALERT_SENT_KEY',
    'SYNC_ERROR_KEY',
    'SYNC_FINISHED_KEY',
    'SYNC_HISTORY_KEY',
    'SYNC_LOGIN_REQUIRED_AT_KEY',
    'SYNC_SETTINGS_KEY',
    'SYNC_STARTED_KEY',
    'SYNC_STATUS_KEY',
    '_get_window_hours',
    '_is_within_sync_window',
    '_persist_sync_outcome',
    '_should_skip_for_login',
    '_to_utc_timestamp',
    '_today_str',
    'append_sync_history',
    'default_sync_settings',
    'get_sync_settings',
    'get_sync_status',
    'set_sync_settings',
    'set_sync_state',
]
