"""Sync settings, status, history, and alert management."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from .config import DEFAULT_SYNC_REQUEST_INTERVAL
from .emailer import get_email_settings, send_email
from .logger import get_logger
from .storage import PostgresStorage, load_meta_json, save_meta_json
from .sync_types import SyncReport
from .utils import to_utc_dt

SYNC_STATUS_KEY = 'sync:last_status'
SYNC_ERROR_KEY = 'sync:last_error'
SYNC_STARTED_KEY = 'sync:last_started_at'
SYNC_FINISHED_KEY = 'sync:last_finished_at'
SYNC_HISTORY_KEY = 'sync:history'
SYNC_SETTINGS_KEY = 'sync:settings'
ALERT_SENT_KEY = 'sync:alert_sent'
# worker 侧写的运行态：心跳 + 队列水位（web 轮询直接读 meta，不扫大表）
WORKER_HEARTBEAT_KEY = 'sync:worker_heartbeat_at'
QUEUE_STATS_KEY = 'sync:queue_stats'
#: 最近一次真正把文章落库的时刻。公众号推送是实时入队的，列表同步 job 可能
#: 很久才跑一次，只用 SYNC_FINISHED_KEY 判断新鲜度会长期显示成几天前。
SYNC_INGEST_KEY = 'sync:last_ingest_at'

_ARTICLE_EXCLUDE_KEYWORD_LIMIT = 20

_logger = get_logger(__name__)


def default_sync_settings() -> dict[str, Any]:
    return {
        'enabled': False,
        'interval_minutes': 60,
        'sleep_seconds': DEFAULT_SYNC_REQUEST_INTERVAL,
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


async def get_sync_settings(storage: PostgresStorage) -> dict[str, Any]:
    settings = await load_meta_json(storage, SYNC_SETTINGS_KEY, default_sync_settings())
    defaults = default_sync_settings()
    # 只认默认值里有的键：老配置里留下的 window_start_hour / window_end_hour 会被丢掉
    merged = {**defaults, **{key: value for key, value in (settings or {}).items() if key in defaults}}
    merged['article_exclude_keywords'] = _normalize_article_exclude_keywords(
        merged.get('article_exclude_keywords'),
    )
    return merged


async def set_sync_settings(storage: PostgresStorage, updates: dict[str, Any]) -> dict[str, Any]:
    current = await get_sync_settings(storage)
    current.update(updates)
    if 'article_exclude_keywords' in current:
        current['article_exclude_keywords'] = _normalize_article_exclude_keywords(
            current.get('article_exclude_keywords'),
        )
    async with storage.transaction():
        await save_meta_json(storage, SYNC_SETTINGS_KEY, current)
    return current


async def append_sync_history(storage: PostgresStorage, entry: dict[str, Any]) -> None:
    history = await load_meta_json(storage, SYNC_HISTORY_KEY, [])
    if not isinstance(history, list):
        history = []
    history.insert(0, entry)
    history = history[:50]
    async with storage.transaction():
        await save_meta_json(storage, SYNC_HISTORY_KEY, history)


async def _send_sync_alert(
    storage: PostgresStorage,
    *,
    status: str,
    error: str,
    started_at: str,
    finished_at: str,
    report: SyncReport | None = None,
) -> None:
    if not error or await storage.meta.get(ALERT_SENT_KEY):
        return
    sync_settings = await get_sync_settings(storage)
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
        email_settings = await get_email_settings(storage)
        send_email(email_settings, to_email=str(sync_settings.get('alert_email')), subject=subject, body=body)
        async with storage.transaction():
            await storage.meta.set(ALERT_SENT_KEY, '1')
    except Exception as exc:
        _logger.warning('Failed to send alert email: %s', exc)


async def get_sync_status(storage: PostgresStorage) -> dict[str, Any]:
    return {
        'status': await storage.meta.get(SYNC_STATUS_KEY) or 'idle',
        'last_started_at': await storage.meta.get(SYNC_STARTED_KEY),
        'last_finished_at': await storage.meta.get(SYNC_FINISHED_KEY),
        'last_ingest_at': await storage.meta.get(SYNC_INGEST_KEY),
        'last_error': await storage.meta.get(SYNC_ERROR_KEY),
        'history': await load_meta_json(storage, SYNC_HISTORY_KEY, []),
    }


async def mark_content_ingested(storage: PostgresStorage, *, at: str | None = None) -> None:
    """Record that article rows were just stored, whatever path did it."""
    async with storage.transaction():
        await storage.meta.set(SYNC_INGEST_KEY, at or datetime.now(UTC).isoformat())


async def set_sync_state(
    storage: PostgresStorage,
    *,
    status: str | None = None,
    error: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> None:
    async with storage.transaction():
        if status is not None:
            await storage.meta.set(SYNC_STATUS_KEY, status)
        if error is not None:
            await storage.meta.set(SYNC_ERROR_KEY, error)
        if started_at is not None:
            await storage.meta.set(SYNC_STARTED_KEY, started_at)
        if finished_at is not None:
            await storage.meta.set(SYNC_FINISHED_KEY, finished_at)


async def _persist_sync_outcome(
    storage: PostgresStorage,
    *,
    started_at: str,
    finished_at: str,
    error: str | None,
    report: SyncReport,
) -> dict[str, Any]:
    if error:
        cancelled = error == 'Cancelled by user'
        status = 'cancelled' if cancelled else 'failed'
        await set_sync_state(storage, status=status, error='' if cancelled else error, finished_at=finished_at)
    else:
        status = 'success'
        cancelled = False
        await set_sync_state(storage, status='success', error='', finished_at=finished_at)
        async with storage.transaction():
            await storage.meta.delete(ALERT_SENT_KEY)

    skipped_accounts = sum(
        1 for item in report.details if item.skipped and item.skip_reason in ('disabled', 'recently_synced')
    )
    failed_accounts = sum(1 for item in report.details if item.failed)
    await append_sync_history(
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
        await _send_sync_alert(
            storage,
            status=status,
            error=error or '',
            started_at=started_at,
            finished_at=finished_at,
            report=report,
        )
    return await get_sync_status(storage)


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return to_utc_dt(parsed)


def _to_utc_timestamp(value: datetime | None) -> int | None:
    if not value:
        return None
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return int(value.timestamp())


def _today_str() -> str:
    return datetime.now(UTC).date().isoformat()


__all__ = [
    'ALERT_SENT_KEY',
    'QUEUE_STATS_KEY',
    'SYNC_ERROR_KEY',
    'SYNC_FINISHED_KEY',
    'SYNC_HISTORY_KEY',
    'SYNC_INGEST_KEY',
    'SYNC_SETTINGS_KEY',
    'SYNC_STARTED_KEY',
    'SYNC_STATUS_KEY',
    'WORKER_HEARTBEAT_KEY',
    '_persist_sync_outcome',
    '_to_utc_timestamp',
    '_today_str',
    'append_sync_history',
    'default_sync_settings',
    'get_sync_settings',
    'get_sync_status',
    'mark_content_ingested',
    'set_sync_settings',
    'set_sync_state',
]
