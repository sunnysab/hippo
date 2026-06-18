"""Shared sync core for account article synchronization."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .logger import get_logger
from .models import AccountCredential, ArticleRecord
from .storage import PostgresStorage
from .sync_types import NullSyncObserver, SyncConfig, SyncObserver, SyncPlan, SyncSummary
from .wechat_api import WeChatApiClient, parse_appmsg_publish


class SyncInterrupted(RuntimeError):
    pass


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
    hints = (
        'login',
        'invalid token',
        'invalid session',
        'session',
        'expire',
        'expired',
        'timeout',
    )
    return any(hint in lowered for hint in hints)


def is_freq_control(message: str) -> bool:
    lowered = message.lower()
    hints = ('freq', 'frequency', 'control', 'too fast', 'too frequent')
    return any(hint in lowered for hint in hints)


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
    offset = 0
    if resume_key:
        saved_offset = storage.meta.get(resume_key)
        if saved_offset and saved_offset.isdigit():
            offset = int(saved_offset)
            observer.on_log(f'检测到断点进度，继续 {account.nickname} offset={offset}')
    if offset > 0:
        observer.on_progress(current=offset, total=None, delta=0)

    total_saved = 0
    page_count = 0
    total_count: int | None = None
    completed = False
    request_count = 0
    current_progress = offset

    while True:
        if _get_cancel_event().is_set():
            raise SyncInterrupted('sync cancelled')
        try:
            attempt = 0
            freq_attempt = 0
            while True:
                try:
                    payload = await client.fetch_appmsg_publish(
                        session, fakeid=account.biz, begin=offset, count=page_size
                    )
                    break
                except RuntimeError as exc:
                    message = str(exc)
                    if is_login_error(message):
                        if on_login_required and not on_login_required():
                            raise
                        if login_flow is None:
                            raise
                        if _get_cancel_event().is_set():
                            raise SyncInterrupted('sync cancelled')
                        await login_flow(timeout=300, poll_interval=2)
                        session = storage.sessions.get_login_session()
                        continue
                    if is_freq_control(message):
                        freq_attempt += 1
                        if freq_attempt > 10:
                            raise RuntimeError(f'频控重试次数过多 ({freq_attempt})，终止同步') from exc
                        wait_seconds = 15 if freq_attempt == 1 else min(15 + 5 * (freq_attempt - 1), 60)
                        observer.on_log(f'触发频率控制，等待 {wait_seconds} 秒后重试')
                        await asyncio.sleep(wait_seconds)
                        if _get_cancel_event().is_set():
                            raise SyncInterrupted('sync cancelled')
                        continue
                    raise
                except (httpx.ReadTimeout, httpx.TimeoutException, httpx.TransportError) as exc:
                    attempt += 1
                    if attempt >= 3:
                        raise RuntimeError(f'网络请求超时或失败：{exc}') from exc
                    await asyncio.sleep(min(2**attempt, 5))
                    if _get_cancel_event().is_set():
                        raise SyncInterrupted('sync cancelled')
            request_count += 1
            if request_count % 60 == 0:
                observer.on_log('达到 60 次请求，等待 15 秒')
                await asyncio.sleep(15)
            publish_page = extract_publish_page(payload)
            publish_list = publish_page.get('publish_list') or []
            publish_list_len = len(publish_list)
            total_count = extract_publish_total(payload) or total_count
            if publish_list_len == 0:
                completed = True
                break
            records = parse_appmsg_publish(account.biz, payload)
            stop_due_to_since = False
            if records and (since_timestamp is not None or until_timestamp is not None):
                filtered: list[ArticleRecord] = []
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
                records = filtered
                if stop_due_to_since and not records:
                    completed = True
                    break
            existing_ids: set[str] = set()
            should_check_existing = collect_existing_ids or full_synced_hint or stop_on_existing
            if records and should_check_existing:
                try:
                    existing_ids = storage.articles.get_existing_article_ids(
                        account.biz, [record.article_id for record in records]
                    )
                except Exception as exc:
                    with contextlib.suppress(Exception):
                        storage.rollback()
                    logger.warning(
                        'Failed to query existing article IDs (biz=%s): %s',
                        account.biz,
                        exc,
                    )
                    existing_ids = set()
            if (
                (full_synced_hint or stop_on_existing)
                and records
                and existing_ids
                and len(existing_ids) == len(records)
            ):
                completed = True
                break
            saved = 0
            page_count += 1
            current_completed = offset + publish_list_len
            if total_count is not None and current_completed > total_count:
                current_completed = total_count
            next_offset = offset + page_size
            if records or resume_key:
                with storage.transaction():
                    if records:
                        saved = storage.articles.save_articles(records)
                        storage.accounts.update_last_synced(account.biz)
                        total_saved += saved
                    if resume_key:
                        storage.meta.set(resume_key, str(next_offset))
            offset = next_offset
            delta = current_completed - current_progress
            current_progress = current_completed
            observer.on_page(
                {
                    'records': records,
                    'existing_ids': existing_ids,
                    'saved': saved,
                    'page_count': page_count,
                    'offset': offset,
                    'total_count': total_count,
                    'current': current_completed,
                    'delta': delta,
                }
            )
            observer.on_progress(
                current=current_completed,
                total=total_count,
                delta=max(delta, 0),
            )
            if stop_due_to_since:
                completed = True
                break
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)
        except KeyboardInterrupt as exc:
            raise SyncInterrupted() from exc

    if resume_key and completed:
        with storage.transaction():
            storage.meta.delete(resume_key)
    if total_count is not None and completed:
        observer.on_progress(current=total_count, total=total_count, delta=0)
    summary = SyncSummary(total_saved=total_saved, page_count=page_count, completed=completed)
    observer.on_complete(summary)
    return summary
