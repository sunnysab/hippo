"""Shared sync core for account article synchronization."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .exceptions import SyncInterrupted
from .logger import get_logger
from .models import AccountCredential, ArticleRecord
from .storage import PostgresStorage, open_storage
from .sync_types import NullSyncObserver, SyncConfig, SyncObserver, SyncPlan, SyncSummary
from .wechat_api import WeChatApiClient, parse_appmsg_publish

# --- constants ---------------------------------------------------------------

_MAX_FREQ_RETRIES = 10
_MAX_NETWORK_RETRIES = 3
_FREQ_BACKOFF_BASE = 15
_FREQ_BACKOFF_MAX = 60
_FREQ_BACKOFF_STEP = 5
_BATCH_THROTTLE_REQUESTS = 60
_BATCH_THROTTLE_SECONDS = 15
_LOGIN_FLOW_TIMEOUT = 300
_LOGIN_FLOW_POLL_INTERVAL = 2
_NETWORK_BACKOFF_MAX = 5

# --- cancel event ------------------------------------------------------------

_cancel_event: asyncio.Event | None = None


def _get_cancel_event() -> asyncio.Event:
    global _cancel_event
    if _cancel_event is None:
        _cancel_event = asyncio.Event()
    return _cancel_event


def request_sync_cancel() -> None:
    _get_cancel_event().set()


def reset_sync_cancel() -> None:
    _get_cancel_event().clear()


logger = get_logger(__name__)

# --- payload helpers ---------------------------------------------------------


def extract_publish_total(payload: dict[str, Any]) -> int | None:
    raw_page = payload.get('publish_page')
    if isinstance(raw_page, str) and raw_page:
        try:
            parsed = json.loads(raw_page)
        except json.JSONDecodeError:
            parsed = {}
        total = parsed.get('total_count')
        if isinstance(total, int):
            return total
        if isinstance(total, str) and total.isdigit():
            return int(total)
    total = payload.get('total_count')
    if isinstance(total, int):
        return total
    if isinstance(total, str) and total.isdigit():
        return int(total)
    return None


def extract_publish_page(payload: dict[str, Any]) -> dict[str, Any]:
    raw_page = payload.get('publish_page')
    if isinstance(raw_page, str) and raw_page:
        try:
            parsed = json.loads(raw_page)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def is_login_error(message: str) -> bool:
    lowered = message.lower()
    return any(hint in lowered for hint in ('login', 'invalid token', 'invalid session', 'session', 'expire', 'expired', 'timeout'))


def is_freq_control(message: str) -> bool:
    lowered = message.lower()
    return any(hint in lowered for hint in ('freq', 'frequency', 'control', 'too fast', 'too frequent'))


# --- fetch with retry --------------------------------------------------------


async def _fetch_with_retry(
    *,
    storage: PostgresStorage,
    client: WeChatApiClient,
    session: Any,
    account: AccountCredential,
    offset: int,
    page_size: int,
    login_flow: Callable[..., Awaitable[None]] | None,
    on_login_required: Callable[[], bool] | None,
    observer: SyncObserver,
) -> tuple[dict[str, Any], Any]:
    """Fetch a single page with retry logic for login, freq control, and network errors.

    Returns (payload, updated_session).
    """
    attempt = 0
    freq_attempt = 0
    while True:
        try:
            payload = await client.fetch_appmsg_publish(session, fakeid=account.biz, begin=offset, count=page_size)
            return payload, session
        except RuntimeError as exc:
            message = str(exc)
            if is_login_error(message):
                if on_login_required and not on_login_required():
                    raise
                if login_flow is None:
                    raise
                _check_cancel()
                await login_flow(timeout=_LOGIN_FLOW_TIMEOUT, poll_interval=_LOGIN_FLOW_POLL_INTERVAL)
                session = storage.sessions.get_login_session()
                continue
            if is_freq_control(message):
                freq_attempt += 1
                if freq_attempt > _MAX_FREQ_RETRIES:
                    raise RuntimeError(f'频控重试次数过多 ({freq_attempt})，终止同步') from exc
                wait = _FREQ_BACKOFF_BASE if freq_attempt == 1 else min(_FREQ_BACKOFF_BASE + _FREQ_BACKOFF_STEP * (freq_attempt - 1), _FREQ_BACKOFF_MAX)
                observer.on_log(f'触发频率控制，等待 {wait} 秒后重试')
                await asyncio.sleep(wait)
                _check_cancel()
                continue
            raise
        except (httpx.ReadTimeout, httpx.TimeoutException, httpx.TransportError) as exc:
            attempt += 1
            if attempt >= _MAX_NETWORK_RETRIES:
                raise RuntimeError(f'网络请求超时或失败：{exc}') from exc
            await asyncio.sleep(min(2**attempt, _NETWORK_BACKOFF_MAX))
            _check_cancel()


def _check_cancel() -> None:
    if _get_cancel_event().is_set():
        raise SyncInterrupted('sync cancelled')


# --- record filtering --------------------------------------------------------


def _filter_records_by_time(
    records: list[ArticleRecord],
    *,
    since_timestamp: int | None,
    until_timestamp: int | None,
) -> tuple[list[ArticleRecord], bool]:
    """Filter records by time window. Returns (filtered_records, stop_due_to_since)."""
    if not records or (since_timestamp is None and until_timestamp is None):
        return records, False
    filtered: list[ArticleRecord] = []
    stop_due_to_since = False
    for record in records:
        publish_at = record.publish_at
        if until_timestamp is not None and publish_at is not None and publish_at > until_timestamp:
            continue
        if since_timestamp is not None:
            if publish_at is None or publish_at >= since_timestamp:
                filtered.append(record)
                continue
            stop_due_to_since = True
            break
        filtered.append(record)
    return filtered, stop_due_to_since


# --- existing ID check -------------------------------------------------------


def _check_existing_ids(
    storage: PostgresStorage,
    biz: str,
    records: list[ArticleRecord],
    *,
    collect_existing_ids: bool,
    full_synced_hint: bool,
    stop_on_existing: bool,
) -> tuple[set[str], bool]:
    """Check for existing articles. Returns (existing_ids, should_stop)."""
    should_check = collect_existing_ids or full_synced_hint or stop_on_existing
    if not records or not should_check:
        return set(), False
    try:
        existing_ids = storage.articles.get_existing_article_ids(biz, [r.article_id for r in records])
    except Exception as exc:
        with contextlib.suppress(Exception):
            storage.rollback()
        logger.warning('Failed to query existing article IDs (biz=%s): %s', biz, exc)
        existing_ids = set()
    if (full_synced_hint or stop_on_existing) and existing_ids and len(existing_ids) == len(records):
        return existing_ids, True
    return existing_ids, False


# --- main sync loop ----------------------------------------------------------


async def sync_account_core(
    *,
    storage: PostgresStorage,
    client: WeChatApiClient,
    account: AccountCredential,
    config: SyncConfig,
    plan: SyncPlan,
    login_flow: Callable[..., Awaitable[None]] | None = None,
    on_login_required: Callable[[], bool] | None = None,
    collect_existing_ids: bool = False,
    observer: SyncObserver | None = None,
) -> SyncSummary:
    observer = observer or NullSyncObserver()
    page_size = config.page_size
    sleep_seconds = config.sleep_seconds
    resume_key = plan.resume_key
    full_synced_hint = plan.full_synced_hint
    since_timestamp = plan.since_timestamp
    until_timestamp = plan.until_timestamp
    stop_on_existing = plan.stop_on_existing

    session = storage.sessions.get_login_session()
    offset = _restore_offset(storage, resume_key, observer, account)

    total_saved = 0
    page_count = 0
    total_count: int | None = None
    completed = False
    request_count = 0
    current_progress = offset

    while True:
        _check_cancel()
        try:
            payload, session = await _fetch_with_retry(
                storage=storage, client=client, session=session, account=account, offset=offset, page_size=page_size,
                login_flow=login_flow, on_login_required=on_login_required, observer=observer,
            )
            request_count += 1
            if request_count % _BATCH_THROTTLE_REQUESTS == 0:
                observer.on_log(f'达到 {_BATCH_THROTTLE_REQUESTS} 次请求，等待 {_BATCH_THROTTLE_SECONDS} 秒')
                await asyncio.sleep(_BATCH_THROTTLE_SECONDS)

            publish_page = extract_publish_page(payload)
            publish_list_len = len(publish_page.get('publish_list') or [])
            total_count = extract_publish_total(payload) or total_count
            if publish_list_len == 0:
                completed = True
                break

            records = parse_appmsg_publish(account.biz, payload)
            records, stop_due_to_since = _filter_records_by_time(records, since_timestamp=since_timestamp, until_timestamp=until_timestamp)
            if stop_due_to_since and not records:
                completed = True
                break

            existing_ids, should_stop = _check_existing_ids(
                storage, account.biz, records,
                collect_existing_ids=collect_existing_ids, full_synced_hint=full_synced_hint, stop_on_existing=stop_on_existing,
            )
            if should_stop:
                completed = True
                break

            saved, page_count, total_saved = await _save_page(records, resume_key, offset + page_size, account.biz, page_count, total_saved)
            current_completed = min(offset + publish_list_len, total_count) if total_count else offset + publish_list_len
            delta = current_completed - current_progress
            current_progress = current_completed
            _emit_progress(observer, records, existing_ids, saved, page_count, offset + page_size, total_count, current_completed, delta)

            if stop_due_to_since:
                completed = True
                break
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)
            offset += page_size
        except KeyboardInterrupt as exc:
            raise SyncInterrupted() from exc

    _finalize(storage, resume_key, completed, total_count, observer, total_saved, page_count)
    return SyncSummary(total_saved=total_saved, page_count=page_count, completed=completed)


def _restore_offset(storage: PostgresStorage, resume_key: str | None, observer: SyncObserver, account: AccountCredential) -> int:
    if not resume_key:
        return 0
    saved_offset = storage.meta.get(resume_key)
    if saved_offset and saved_offset.isdigit():
        offset = int(saved_offset)
        observer.on_log(f'检测到断点进度，继续 {account.nickname} offset={offset}')
        observer.on_progress(current=offset, total=None, delta=0)
        return offset
    return 0


async def _save_page(
    records: list[ArticleRecord], resume_key: str | None,
    next_offset: int, biz: str, page_count: int, total_saved: int,
) -> tuple[int, int, int]:
    def _do_save() -> int:
        with open_storage() as ts:
            saved = 0
            with ts.transaction():
                if records:
                    saved = ts.articles.save_articles(records)
                    ts.accounts.update_last_synced(biz)
                if resume_key:
                    ts.meta.set(resume_key, str(next_offset))
            return saved
    saved = await asyncio.to_thread(_do_save)
    return saved, page_count + 1, total_saved + saved


def _emit_progress(
    observer: SyncObserver, records: list[ArticleRecord], existing_ids: set[str],
    saved: int, page_count: int, offset: int, total_count: int | None,
    current: int, delta: int,
) -> None:
    observer.on_page({
        'records': records, 'existing_ids': existing_ids, 'saved': saved,
        'page_count': page_count, 'offset': offset, 'total_count': total_count,
        'current': current, 'delta': delta,
    })
    observer.on_progress(current=current, total=total_count, delta=max(delta, 0))


def _finalize(storage: PostgresStorage, resume_key: str | None, completed: bool, total_count: int | None, observer: SyncObserver, total_saved: int, page_count: int) -> None:
    if resume_key and completed:
        with storage.transaction():
            storage.meta.delete(resume_key)
    if total_count is not None and completed:
        observer.on_progress(current=total_count, total=total_count, delta=0)
    observer.on_complete(SyncSummary(total_saved=total_saved, page_count=page_count, completed=completed))
