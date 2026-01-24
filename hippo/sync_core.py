"""Shared sync core for account article synchronization."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Awaitable, Callable, Literal

import httpx

from .http import MPClient, parse_appmsg_publish
from .models import AccountCredential, ArticleRecord
from .storage import PostgresStorage

SyncEventType = Literal["progress", "log", "page", "complete"]
SyncEvent = tuple[SyncEventType, Any]


class SyncInterrupted(RuntimeError):
    pass


def extract_publish_total(payload: dict[str, Any]) -> int | None:
    raw_page = payload.get("publish_page")
    if isinstance(raw_page, str) and raw_page:
        try:
            parsed = json.loads(raw_page)
        except json.JSONDecodeError:
            parsed = {}
        total = parsed.get("total_count")
        if isinstance(total, int):
            return total
        if isinstance(total, str) and total.isdigit():
            return int(total)
    total = payload.get("total_count")
    if isinstance(total, int):
        return total
    if isinstance(total, str) and total.isdigit():
        return int(total)
    return None


def extract_publish_page(payload: dict[str, Any]) -> dict[str, Any]:
    raw_page = payload.get("publish_page")
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
    hints = ("login", "token", "session", "invalid", "expire", "expired", "timeout")
    return any(hint in lowered for hint in hints)


def is_freq_control(message: str) -> bool:
    lowered = message.lower()
    hints = ("freq", "frequency", "control", "too fast", "too frequent")
    return any(hint in lowered for hint in hints)


async def sync_account_core(
    *,
    storage: PostgresStorage,
    client: MPClient,
    account: AccountCredential,
    page_size: int,
    pages: int | None,
    sleep_seconds: float,
    resume_key: str | None = None,
    full_synced_hint: bool = False,
    since_timestamp: int | None = None,
    until_timestamp: int | None = None,
    stop_on_existing: bool = False,
    login_flow: Callable[..., Awaitable[None]] | None = None,
    on_login_required: Callable[[], bool] | None = None,
    collect_existing_ids: bool = False,
) -> AsyncGenerator[SyncEvent, None]:
    session = storage.get_login_session()
    offset = 0
    if resume_key:
        saved_offset = storage.get_meta(resume_key)
        if saved_offset and saved_offset.isdigit():
            offset = int(saved_offset)
            yield "log", f"检测到断点进度，继续 {account.nickname} offset={offset}"
    if offset > 0:
        yield "progress", {"current": offset, "total": None, "delta": 0}

    total_saved = 0
    page_count = 0
    total_count: int | None = None
    completed = False
    request_count = 0
    current_progress = offset

    while True:
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
                        await login_flow(timeout=300, poll_interval=2)
                        session = storage.get_login_session()
                        continue
                    if is_freq_control(message):
                        freq_attempt += 1
                        wait_seconds = 15 if freq_attempt == 1 else min(15 + 5 * (freq_attempt - 1), 60)
                        yield "log", f"触发频率控制，等待 {wait_seconds} 秒后重试"
                        await asyncio.sleep(wait_seconds)
                        continue
                    raise
                except (httpx.ReadTimeout, httpx.TimeoutException, httpx.TransportError) as exc:
                    attempt += 1
                    if attempt >= 3:
                        raise RuntimeError(f"网络请求超时或失败：{exc}") from exc
                    await asyncio.sleep(min(2**attempt, 5))
            request_count += 1
            if request_count % 60 == 0:
                yield "log", "达到 60 次请求，等待 15 秒"
                await asyncio.sleep(15)
            publish_page = extract_publish_page(payload)
            publish_list = publish_page.get("publish_list") or []
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
                    if (
                        until_timestamp is not None
                        and publish_at is not None
                        and publish_at > until_timestamp
                    ):
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
                    existing_ids = storage.get_existing_article_ids(
                        account.biz, [record.article_id for record in records]
                    )
                except Exception:
                    existing_ids = set()
            if (full_synced_hint or stop_on_existing) and records and existing_ids and len(existing_ids) == len(records):
                completed = True
                break
            saved = 0
            if records:
                saved = storage.save_articles(records)
                storage.update_last_synced(account.biz)
                total_saved += saved
            page_count += 1
            current_completed = offset + publish_list_len
            if total_count is not None and current_completed > total_count:
                current_completed = total_count
            offset += page_size
            if resume_key:
                storage.set_meta(resume_key, str(offset))
            delta = current_completed - current_progress
            current_progress = current_completed
            yield "page", {
                "records": records,
                "existing_ids": existing_ids,
                "saved": saved,
                "page_count": page_count,
                "offset": offset,
                "total_count": total_count,
                "current": current_completed,
                "delta": delta,
            }
            yield "progress", {
                "current": current_completed,
                "total": total_count,
                "delta": max(delta, 0),
            }
            if stop_due_to_since:
                completed = True
                break
            if pages is not None and page_count >= pages:
                completed = False
                break
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)
        except KeyboardInterrupt as exc:
            raise SyncInterrupted() from exc

    if resume_key and completed:
        storage.delete_meta(resume_key)
    if total_count is not None and completed:
        yield "progress", {"current": total_count, "total": total_count, "delta": 0}
    yield "complete", {"total_saved": total_saved, "page_count": page_count, "completed": completed}
