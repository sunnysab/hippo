"""Minimal HTTP server for Hippo API + static UI."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import smtplib
import threading
import time as time_module
from datetime import date, datetime, time, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Generator, Optional

import httpx
import psycopg2
import psycopg2.extras
from fastapi import APIRouter, Body, Depends, FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

try:
    import jieba
except Exception:  # pragma: no cover - optional fallback
    jieba = None

from .downloader import ArticleDownloader
from .http import MPClient, parse_appmsg_publish
from .models import AccountCredential, ArticleRecord
from .rss import build_rss_xml, query_rss_items
from .storage import StorageLike, open_storage

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_GROUP_NAME = "Default"
SYNC_STATUS_KEY = "sync:last_status"
SYNC_ERROR_KEY = "sync:last_error"
SYNC_STARTED_KEY = "sync:last_started_at"
SYNC_FINISHED_KEY = "sync:last_finished_at"
SYNC_HISTORY_KEY = "sync:history"
SYNC_SETTINGS_KEY = "sync:settings"
ALERT_SENT_KEY = "sync:alert_sent"

logger = logging.getLogger("hippo.serve")


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status




def _parse_int(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(f"Invalid integer: {value}") from exc


_SYNC_MODES = {'incremental', 'recent', 'full', 'range'}


def _normalize_sync_mode(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    mode = str(value).strip().lower()
    if not mode:
        return None
    if mode not in _SYNC_MODES:
        raise ApiError('Invalid sync mode', status=400)
    return mode


def _normalize_recent_days(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError('Invalid recent days') from exc
    if days < 1:
        raise ApiError('Invalid recent days', status=400)
    return days


def _parse_date(value: Optional[str], *, end_of_day: bool = False) -> Optional[int]:
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ApiError(f"Invalid date: {value}") from exc
    dt = datetime.combine(parsed, time.max if end_of_day else time.min)
    return int(dt.timestamp())


def _normalize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: _normalize_value(value) for key, value in record.items()}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_meta_json(storage: StorageLike, key: str, default: Any) -> Any:
    raw = storage.get_meta(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _save_meta_json(storage: StorageLike, key: str, value: Any) -> None:
    storage.set_meta(key, json.dumps(value, ensure_ascii=False))


def _default_sync_settings() -> dict[str, Any]:
    return {
        "enabled": False,
        "interval_minutes": 60,
        "mode": "incremental",
        "recent_days": 7,
        "page_size": 10,
        "page_limit": 2,
        "sleep_seconds": 0.05,
        "download_content": True,
        "download_images": True,
        "content_limit": 20,
        "skip_minutes": 30,
        "alert_enabled": False,
        "alert_email": "",
    }


def _default_email_settings() -> dict[str, Any]:
    return {
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_user": "",
        "smtp_password": "",
        "smtp_tls": True,
        "from_email": "",
    }


def _get_email_settings(storage: StorageLike) -> dict[str, Any]:
    settings = _load_meta_json(storage, "email:settings", _default_email_settings())
    defaults = _default_email_settings()
    return {**defaults, **(settings or {})}


def _set_email_settings(storage: StorageLike, updates: dict[str, Any]) -> dict[str, Any]:
    current = _get_email_settings(storage)
    current.update(updates)
    _save_meta_json(storage, "email:settings", current)
    return current


def _send_alert_email(storage: StorageLike, subject: str, body: str) -> None:
    settings = _get_email_settings(storage)
    sync_settings = _get_sync_settings(storage)
    if not sync_settings.get("alert_enabled"):
        return
    to_email = sync_settings.get("alert_email") or ""
    if not to_email:
        return
    if not settings.get("smtp_host"):
        return
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.get("from_email") or settings.get("smtp_user") or to_email
    message["To"] = to_email
    message.set_content(body)
    smtp_host = settings.get("smtp_host")
    smtp_port = int(settings.get("smtp_port") or 587)
    smtp_user = settings.get("smtp_user")
    smtp_password = settings.get("smtp_password")
    use_tls = bool(settings.get("smtp_tls"))
    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
        if use_tls:
            smtp.starttls()
        if smtp_user:
            smtp.login(smtp_user, smtp_password or "")
        smtp.send_message(message)


def _get_sync_settings(storage: StorageLike) -> dict[str, Any]:
    settings = _load_meta_json(storage, SYNC_SETTINGS_KEY, _default_sync_settings())
    defaults = _default_sync_settings()
    merged = {**defaults, **(settings or {})}
    return merged


def _set_sync_settings(storage: StorageLike, updates: dict[str, Any]) -> dict[str, Any]:
    current = _get_sync_settings(storage)
    current.update(updates)
    _save_meta_json(storage, SYNC_SETTINGS_KEY, current)
    return current


def _append_sync_history(storage: StorageLike, entry: dict[str, Any]) -> None:
    history = _load_meta_json(storage, SYNC_HISTORY_KEY, [])
    if not isinstance(history, list):
        history = []
    history.insert(0, entry)
    history = history[:50]
    _save_meta_json(storage, SYNC_HISTORY_KEY, history)


def _get_sync_status(storage: StorageLike) -> dict[str, Any]:
    return {
        "status": storage.get_meta(SYNC_STATUS_KEY) or "idle",
        "last_started_at": storage.get_meta(SYNC_STARTED_KEY),
        "last_finished_at": storage.get_meta(SYNC_FINISHED_KEY),
        "last_error": storage.get_meta(SYNC_ERROR_KEY),
        "history": _load_meta_json(storage, SYNC_HISTORY_KEY, []),
    }


def _set_sync_state(
    storage: StorageLike,
    *,
    status: Optional[str] = None,
    error: Optional[str] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
) -> None:
    if status is not None:
        storage.set_meta(SYNC_STATUS_KEY, status)
    if error is not None:
        storage.set_meta(SYNC_ERROR_KEY, error)
    if started_at is not None:
        storage.set_meta(SYNC_STARTED_KEY, started_at)
    if finished_at is not None:
        storage.set_meta(SYNC_FINISHED_KEY, finished_at)


def _get_login_info(storage: StorageLike) -> Optional[dict[str, Any]]:
    row = _fetchone(
        storage,
        "SELECT nickname, avatar, updated_at FROM login_sessions ORDER BY id DESC LIMIT 1",
        [],
    )
    return row


def _is_login_error(message: str) -> bool:
    lowered = message.lower()
    hints = ("login", "token", "session", "invalid", "expire", "expired", "timeout")
    return any(hint in lowered for hint in hints)


def _is_freq_control(message: str) -> bool:
    lowered = message.lower()
    hints = ("freq", "frequency", "control", "too fast", "too frequent")
    return any(hint in lowered for hint in hints)


class LoginManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._uuid_cookie: Optional[str] = None
        self._qrcode: Optional[bytes] = None
        self._status: str = "idle"
        self._message: str = ""
        self._updated_at: Optional[str] = None

    def _snapshot(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "message": self._message,
            "updated_at": self._updated_at,
            "has_qrcode": self._qrcode is not None,
        }

    async def start(self) -> dict[str, Any]:
        with self._lock:
            if self._status in ("starting", "waiting", "scanned", "refresh"):
                return self._snapshot()
            self._status = "starting"
            self._message = "Requesting QR code"
        sid = f"{int(time_module.time() * 1000)}{random.randint(100, 999)}"
        try:
            async with MPClient(timeout=15.0) as client:
                uuid_cookie = await client.start_login_session(sid)
                qrcode_bytes = await client.fetch_login_qrcode(uuid_cookie)
            with self._lock:
                self._uuid_cookie = uuid_cookie
                self._qrcode = qrcode_bytes
                self._status = "waiting"
                self._message = "Scan the QR code with WeChat"
                self._updated_at = _utc_now_iso()
            return self._snapshot()
        except Exception as exc:
            with self._lock:
                self._status = "error"
                self._message = str(exc)
                self._updated_at = _utc_now_iso()
            raise ApiError(str(exc)) from exc

    def get_qrcode(self) -> bytes:
        with self._lock:
            if not self._qrcode:
                raise ApiError("QR code not ready", status=404)
            return self._qrcode

    async def poll(self, storage: StorageLike) -> dict[str, Any]:
        with self._lock:
            uuid_cookie = self._uuid_cookie
        if not uuid_cookie:
            raise ApiError("Login not started", status=400)
        try:
            async with MPClient(timeout=15.0) as client:
                resp = await client.check_login_status(uuid_cookie)
                if resp.get("base_resp", {}).get("ret") != 0:
                    raise ApiError("Login status error")
                status = resp.get("status")
                if status == 1:
                    session = await client.finalize_login(uuid_cookie)
                    info = await client.fetch_login_info(session)
                    session.nickname = info.get("nickname") or None
                    session.avatar = info.get("avatar") or None
                    storage.save_login_session(session)
                    with self._lock:
                        self._status = "success"
                        self._message = "Login success"
                        self._uuid_cookie = None
                        self._qrcode = None
                        self._updated_at = _utc_now_iso()
                    return self._snapshot()
                if status in (2, 3):
                    qrcode_bytes = await client.fetch_login_qrcode(uuid_cookie)
                    with self._lock:
                        self._qrcode = qrcode_bytes
                        self._status = "refresh"
                        self._message = "QR code refreshed"
                        self._updated_at = _utc_now_iso()
                    return self._snapshot()
                if status in (4, 6):
                    with self._lock:
                        self._status = "scanned"
                        self._message = "Scan success, waiting for confirmation"
                        self._updated_at = _utc_now_iso()
                    return self._snapshot()
                if status == 5:
                    with self._lock:
                        self._status = "error"
                        self._message = "Account cannot login without email"
                        self._updated_at = _utc_now_iso()
                    return self._snapshot()
        except ApiError:
            raise
        except Exception as exc:
            with self._lock:
                self._status = "error"
                self._message = str(exc)
                self._updated_at = _utc_now_iso()
            raise ApiError(str(exc)) from exc
        return self._snapshot()

    def cancel(self) -> None:
        with self._lock:
            self._uuid_cookie = None
            self._qrcode = None
            self._status = "idle"
            self._message = ""
            self._updated_at = _utc_now_iso()


class SyncScheduler:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
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
                settings = _get_sync_settings(storage)
            if not settings.get("enabled"):
                self._trigger.wait(timeout=10)
                self._trigger.clear()
                continue
            interval = max(int(settings.get("interval_minutes") or 1), 1) * 60
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

    def _run_sync(self, *, group_id: Optional[int] = None) -> dict[str, Any]:
        return asyncio.run(self._run_sync_async(group_id=group_id))

    async def _run_sync_async(self, *, group_id: Optional[int] = None) -> dict[str, Any]:
        started_at = _utc_now_iso()
        with open_storage() as storage:
            settings = _get_sync_settings(storage)
            _set_sync_state(storage, status="running", error="", started_at=started_at)
            try:
                storage.get_login_session()
            except Exception as exc:
                error = str(exc)
                _set_sync_state(storage, status="login_required", error=error, finished_at=_utc_now_iso())
                _append_sync_history(
                    storage,
                    {
                        "started_at": started_at,
                        "finished_at": _utc_now_iso(),
                        "status": "login_required",
                        "error": error,
                    },
                )
                return _get_sync_status(storage)

            accounts = storage.list_accounts()
            if group_id is not None:
                accounts = [account for account in accounts if account.group_id == group_id]
            group_defaults: dict[int, dict[str, Any]] = {}
            group_rows = _fetchall(
                storage,
                'SELECT id, sync_mode, sync_recent_days FROM account_groups',
                [],
            )
            for row in group_rows:
                group_defaults[int(row['id'])] = row
            total_saved = 0
            total_downloaded = 0
            skipped_accounts = 0
            error: Optional[str] = None
            async with MPClient() as client:
                async with ArticleDownloader(
                    client=client,
                    storage=storage,
                    enable_image_worker=bool(settings.get("download_images")),
                ) as downloader:
                    for account in accounts:
                        if account.is_disabled:
                            skipped_accounts += 1
                            continue
                        if _should_skip_by_time(account.last_synced_at, settings.get("skip_minutes")):
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
                            if _is_login_error(message):
                                error = message
                                break
                            if _is_freq_control(message):
                                await asyncio.sleep(15)
                                continue
                            error = message
                            break
                        total_saved += saved
                        total_downloaded += downloaded
                    if settings.get("download_images"):
                        await downloader.wait_for_images()

            finished_at = _utc_now_iso()
            if error:
                status = "login_required" if _is_login_error(error) else "failed"
                _set_sync_state(storage, status=status, error=error, finished_at=finished_at)
            else:
                _set_sync_state(storage, status="success", error="", finished_at=finished_at)
                storage.delete_meta(ALERT_SENT_KEY)
            _append_sync_history(
                storage,
                {
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "status": "login_required" if error and _is_login_error(error) else ("failed" if error else "success"),
                    "error": error or "",
                    "saved": total_saved,
                    "downloaded": total_downloaded,
                    "skipped_accounts": skipped_accounts,
                },
            )
            if error and not storage.get_meta(ALERT_SENT_KEY):
                subject = "Hippo sync failed"
                body = f"Status: {status}\\nError: {error}\\nStarted: {started_at}\\nFinished: {finished_at}"
                try:
                    _send_alert_email(storage, subject, body)
                    storage.set_meta(ALERT_SENT_KEY, "1")
                except Exception as exc:
                    logger.warning("Failed to send alert email: %s", exc)
            return _get_sync_status(storage)


def _fetchall(storage: StorageLike, query: str, params: list[Any]) -> list[dict[str, Any]]:
    with storage.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [_normalize_record(dict(row)) for row in rows]


def _fetchone(storage: StorageLike, query: str, params: list[Any]) -> Optional[dict[str, Any]]:
    with storage.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    return _normalize_record(dict(row)) if row else None


def _ensure_default_group(storage: StorageLike) -> dict[str, Any]:
    groups = storage.list_groups()
    default_group = next((g for g in groups if g.name == DEFAULT_GROUP_NAME), None)
    if default_group is None:
        default_group = storage.upsert_group(DEFAULT_GROUP_NAME)
    default_id = default_group.id
    with storage.conn.cursor() as cur:
        cur.execute(
            "UPDATE accounts SET group_id = %s WHERE group_id IS NULL",
            (default_id,),
        )
    storage.conn.commit()
    return {"id": default_id, "name": default_group.name}


def _ensure_avatar_images_table(storage: StorageLike) -> None:
    with storage.conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS avatar_images (
                biz TEXT PRIMARY KEY,
                avatar_url TEXT,
                content_type TEXT,
                data BYTEA,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )
    storage.conn.commit()
    _migrate_legacy_avatar_tables(storage)


def _migrate_legacy_avatar_tables(storage: StorageLike) -> None:
    with storage.conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.account_images')")
        has_account_images = cur.fetchone()[0] is not None
        if has_account_images:
            cur.execute(
                """
                INSERT INTO avatar_images (biz, content_type, data, updated_at)
                SELECT biz, content_type, data, updated_at FROM account_images
                ON CONFLICT (biz) DO NOTHING
                """
            )
        cur.execute("SELECT to_regclass('public.account_search_images')")
        has_search_images = cur.fetchone()[0] is not None
        if has_search_images:
            cur.execute(
                """
                INSERT INTO avatar_images (biz, avatar_url, content_type, data, updated_at)
                SELECT biz, avatar_url, content_type, data, updated_at FROM account_search_images
                ON CONFLICT (biz) DO NOTHING
                """
            )
    storage.conn.commit()


def _should_skip_by_time(last_synced_at: Optional[datetime], skip_minutes: Optional[int]) -> bool:
    if not skip_minutes or skip_minutes <= 0:
        return False
    if not last_synced_at:
        return False
    now = datetime.now(timezone.utc)
    synced_at = last_synced_at
    if synced_at.tzinfo is None:
        synced_at = synced_at.replace(tzinfo=timezone.utc)
    delta = now - synced_at
    return delta.total_seconds() < skip_minutes * 60


def _extract_publish_page(payload: dict[str, Any]) -> dict[str, Any]:
    raw_page = payload.get("publish_page") or "{}"
    try:
        return json.loads(raw_page)
    except json.JSONDecodeError:
        return {}


def _select_missing_content(
    storage: StorageLike,
    biz: str,
    *,
    limit: int,
) -> list[ArticleRecord]:
    if limit <= 0:
        return []
    articles = storage.list_articles(biz, limit=limit)
    get_content_ids = getattr(storage, "get_article_content_ids", None)
    if callable(get_content_ids):
        try:
            ids = get_content_ids(biz, [article.article_id for article in articles])
            return [article for article in articles if article.article_id not in ids]
        except Exception:
            return articles
    return articles


async def _sync_account_articles(
    *,
    storage: StorageLike,
    client: MPClient,
    downloader: ArticleDownloader,
    account: AccountCredential,
    settings: dict[str, Any],
    group_defaults: Optional[dict[int, dict[str, Any]]] = None,
) -> tuple[int, int]:
    page_size = max(int(settings.get("page_size") or 10), 1)
    page_limit = settings.get("page_limit")
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
    since_ts: Optional[int] = None
    until_ts: Optional[int] = None
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

    session = storage.get_login_session()
    offset = 0
    page_count = 0
    total_saved = 0
    to_download: list[ArticleRecord] = []

    while True:
        payload = await client.fetch_appmsg_publish(
            session, fakeid=account.biz, begin=offset, count=page_size
        )
        publish_page = _extract_publish_page(payload)
        publish_list = publish_page.get("publish_list") or []
        if not publish_list:
            break
        records = parse_appmsg_publish(account.biz, payload)
        stop_due_to_since = False
        if records and (since_ts is not None or until_ts is not None):
            filtered: list[ArticleRecord] = []
            for record in records:
                publish_at = record.publish_at
                if (
                    until_ts is not None
                    and publish_at is not None
                    and publish_at > until_ts
                ):
                    continue
                if since_ts is not None:
                    if publish_at is None or publish_at >= since_ts:
                        filtered.append(record)
                        continue
                    stop_due_to_since = True
                    break
                filtered.append(record)
            records = filtered
            if stop_due_to_since and not records:
                break
        existing_ids: set[str] = set()
        get_existing = getattr(storage, "get_existing_article_ids", None)
        if callable(get_existing) and records:
            try:
                existing_ids = set(get_existing(account.biz, [r.article_id for r in records]))
            except Exception:
                existing_ids = set()
        if stop_on_existing and records and existing_ids and len(existing_ids) == len(records):
            break
        if records:
            new_records = [r for r in records if r.article_id not in existing_ids]
            to_download.extend(new_records)
            saved = storage.save_articles(records)
            if saved:
                storage.update_last_synced(account.biz)
                total_saved += saved
        page_count += 1
        offset += page_size
        if stop_due_to_since:
            break
        if page_limit is not None and page_count >= page_limit:
            break
        sleep_seconds = float(settings.get("sleep_seconds") or 0)
        if sleep_seconds > 0:
            await asyncio.sleep(sleep_seconds)

    downloaded = 0
    if settings.get("download_content"):
        content_limit = int(settings.get("content_limit") or 0)
        candidates = {item.article_id: item for item in to_download}
        for missing in _select_missing_content(storage, account.biz, limit=content_limit):
            candidates.setdefault(missing.article_id, missing)
        if candidates:
            results, _, _ = await downloader.download_many(
                candidates.values(),
                with_images=bool(settings.get("download_images")),
                record_images_only=not bool(settings.get("download_images")),
                skip_if_downloaded=True,
            )
            downloaded = len(results)
    return total_saved, downloaded


def _list_groups(storage: StorageLike) -> list[dict[str, Any]]:
    return [
        {
            'id': g.id,
            'name': g.name,
            'account_count': g.account_count,
            'sync_mode': g.sync_mode,
            'sync_recent_days': g.sync_recent_days,
        }
        for g in storage.list_groups()
    ]


def _get_group(storage: StorageLike, group_id: int) -> dict[str, Any]:
    row = _fetchone(
        storage,
        """
        SELECT g.id, g.name, g.sync_mode, g.sync_recent_days, COUNT(a.biz) AS account_count
        FROM account_groups g
        LEFT JOIN accounts a ON a.group_id = g.id
        WHERE g.id = %s
        GROUP BY g.id, g.name, g.sync_mode, g.sync_recent_days
        """,
        [group_id],
    )
    if not row:
        raise ApiError("Group not found", status=404)
    return row


def _update_group(storage: StorageLike, group_id: int, updates: dict[str, Any]) -> dict[str, Any]:
    fields: list[str] = []
    params: list[Any] = []
    mapping = {
        'name': 'name',
        'sync_mode': 'sync_mode',
        'sync_recent_days': 'sync_recent_days',
    }
    for key, column in mapping.items():
        if key in updates:
            fields.append(f"{column} = %s")
            params.append(updates[key])
    if not fields:
        raise ApiError('No fields to update')
    fields.append('updated_at = NOW()')
    params.append(group_id)
    with storage.conn.cursor() as cur:
        cur.execute(
            f"UPDATE account_groups SET {', '.join(fields)} WHERE id = %s",
            params,
        )
        updated = cur.rowcount
    storage.conn.commit()
    if updated == 0:
        raise ApiError('Group not found', status=404)
    return _get_group(storage, group_id)


def _delete_group(storage: StorageLike, group_id: int) -> None:
    default_group = _ensure_default_group(storage)
    default_id = default_group["id"]
    if group_id == default_id:
        raise ApiError("Default group cannot be deleted", status=400)
    with storage.conn.cursor() as cur:
        cur.execute(
            "UPDATE accounts SET group_id = %s WHERE group_id = %s",
            (default_id, group_id),
        )
        cur.execute("DELETE FROM account_groups WHERE id = %s", (group_id,))
        deleted = cur.rowcount
    storage.conn.commit()
    if deleted == 0:
        raise ApiError("Group not found", status=404)


def _build_search_clause(
    *,
    is_postgres: bool,
    terms: list[str],
    fields: list[str],
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for term in terms:
        like = f"%{term}%"
        if is_postgres:
            clause = " OR ".join([f"{field} ILIKE %s" for field in fields])
        else:
            clause = " OR ".join([f"{field} ILIKE %s" for field in fields])
        clauses.append(f"({clause})")
        params.extend([like for _ in fields])
    return " AND ".join(clauses), params


def _tokenize_query(text: str) -> list[str]:
    trimmed = text.strip()
    if not trimmed:
        return []
    if jieba:
        tokens = [token.strip() for token in jieba.lcut(trimmed) if token.strip()]
        seen: set[str] = set()
        ordered: list[str] = []
        for token in tokens:
            if token in seen:
                continue
            seen.add(token)
            ordered.append(token)
        return ordered[:12]
    chunks = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", trimmed)
    tokens: list[str] = []
    for chunk in chunks:
        if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
            tokens.extend(list(chunk))
        else:
            tokens.append(chunk)
    return tokens[:12]


def _list_accounts(
    storage: StorageLike,
    *,
    group_id: Optional[int],
    query: Optional[str],
    page: int,
    page_size: int,
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if group_id is not None:
        where.append("a.group_id = %s")
        params.append(group_id)
    if query:
        tokens = _tokenize_query(query)
        if tokens:
            clause, values = _build_search_clause(
                is_postgres=True,
                terms=tokens,
                fields=["a.nickname", "a.alias", "a.biz"],
            )
            where.append(clause)
            params.extend(values)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    limit_sql = "LIMIT %s OFFSET %s"
    offset = max(page - 1, 0) * page_size
    query_sql = (
        ""
        "SELECT a.biz, a.nickname, a.alias, a.round_head_img, a.group_id,"
        " a.is_disabled, a.last_synced_at, a.sync_mode, a.sync_recent_days, g.name AS group_name,"
        " COALESCE(ac.article_count, 0) AS article_count,"
        " (ai.data IS NOT NULL) AS avatar_ready"
        " FROM accounts a"
        " LEFT JOIN account_groups g ON g.id = a.group_id"
        " LEFT JOIN (SELECT biz, COUNT(*) AS article_count FROM articles GROUP BY biz) ac"
        "   ON ac.biz = a.biz"
        " LEFT JOIN avatar_images ai ON ai.biz = a.biz"
        f" {where_sql}"
        " ORDER BY a.nickname ASC"
        f" {limit_sql}"
    )
    rows = _fetchall(storage, query_sql, params + [page_size, offset])
    for row in rows:
        row["avatar_url"] = f"/api/account/{row['biz']}/avatar"
    count_sql = (
        "SELECT COUNT(*) AS total FROM accounts a"
        " LEFT JOIN account_groups g ON g.id = a.group_id"
        f" {where_sql}"
    )
    total_row = _fetchone(storage, count_sql, params)
    total = int(total_row["total"]) if total_row else 0
    return {"accounts": rows, "page": page, "page_size": page_size, "total": total}


def _get_account(storage: StorageLike, biz: str) -> dict[str, Any]:
    row = _fetchone(
        storage,
        (
            ""
            "SELECT a.biz, a.nickname, a.alias, a.round_head_img, a.group_id,"
            " a.is_disabled, a.last_synced_at, a.sync_mode, a.sync_recent_days, g.name AS group_name,"
            " COALESCE(ac.article_count, 0) AS article_count,"
            " (ai.data IS NOT NULL) AS avatar_ready"
            " FROM accounts a"
            " LEFT JOIN account_groups g ON g.id = a.group_id"
            " LEFT JOIN (SELECT biz, COUNT(*) AS article_count FROM articles GROUP BY biz) ac"
            "   ON ac.biz = a.biz"
            " LEFT JOIN avatar_images ai ON ai.biz = a.biz"
            " WHERE a.biz = %s"
        ),
        [biz],
    )
    if not row:
        raise ApiError("Account not found", status=404)
    row["avatar_url"] = f"/api/account/{row['biz']}/avatar"
    return row


def _update_account(storage: StorageLike, biz: str, payload: dict[str, Any]) -> dict[str, Any]:
    fields: list[str] = []
    params: list[Any] = []
    mapping = {
        'nickname': 'nickname',
        'alias': 'alias',
        'round_head_img': 'round_head_img',
        'group_id': 'group_id',
        'is_disabled': 'is_disabled',
        'sync_mode': 'sync_mode',
        'sync_recent_days': 'sync_recent_days',
    }

    for key, column in mapping.items():
        if key in payload:
            value = payload[key]
            if key == 'is_disabled':
                value = bool(value)
            if key == 'sync_mode':
                value = _normalize_sync_mode(value)
            if key == 'sync_recent_days':
                value = _normalize_recent_days(value)
            fields.append(f"{column} = %s")
            params.append(value)

    if not fields:
        raise ApiError("No fields to update")

    fields.append("updated_at = NOW()")

    params.append(biz)

    query = f"UPDATE accounts SET {', '.join(fields)} WHERE biz = %s"
    with storage.conn.cursor() as cur:
        cur.execute(query, params)
        updated = cur.rowcount
    storage.conn.commit()
    if updated == 0:
        raise ApiError("Account not found", status=404)
    return _get_account(storage, biz)


def _build_article_query(
    *,
    storage: StorageLike,
    group_id: Optional[int],
    biz: Optional[str],
    query: Optional[str],
    since_ts: Optional[int],
    until_ts: Optional[int],
    content_only: bool,
    limit: int,
    offset: int,
    article_id: Optional[str] = None,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    select_params: list[Any] = []
    rank_select = ""
    order_sql = "ORDER BY a.publish_at IS NULL, a.publish_at DESC, a.id DESC"

    if article_id:
        where.append("a.article_id = %s")
        params.append(article_id)

    if group_id is not None:
        where.append("acc.group_id = %s")
        params.append(group_id)
    if biz:
        where.append("a.biz = %s")
        params.append(biz)
    if query:
        query_text = query.strip()
        if query_text:
            where.append("a.search_vector @@ plainto_tsquery('jiebaqry', %s)")
            params.append(query_text)
            rank_select = ", ts_rank(a.search_vector, plainto_tsquery('jiebaqry', %s)) AS rank"
            select_params.append(query_text)
            order_sql = "ORDER BY rank DESC, a.publish_at IS NULL, a.publish_at DESC, a.id DESC"
    if since_ts is not None:
        where.append("a.publish_at >= %s")
        params.append(since_ts)
    if until_ts is not None:
        where.append("a.publish_at <= %s")
        params.append(until_ts)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    limit_sql = "LIMIT %s OFFSET %s"

    image_sql = (
        "LEFT JOIN LATERAL ("
        "  SELECT id FROM article_images i"
        "  WHERE i.article_pk = a.id AND i.data IS NOT NULL"
        "  ORDER BY (i.kind = 'cover') DESC, i.position ASC"
        "  LIMIT 1"
        ") img ON TRUE"
    )
    image_select = "img.id AS image_id"

    content_join = " JOIN article_content ac ON ac.article_pk = a.id" if content_only else ""
    query_sql = (
        "SELECT a.id, a.biz, a.article_id, a.title, a.author, a.digest, a.cover, a.link,"
        " a.source_url, a.publish_at, a.created_at,"
        " acc.nickname AS account_nickname, acc.alias AS account_alias,"
        " acc.round_head_img AS account_avatar,"
        " acc.group_id, g.name AS group_name,"
        f" {image_select}"
        f"{rank_select}"
        " FROM articles a"
        " JOIN accounts acc ON acc.biz = a.biz"
        f"{content_join}"
        " LEFT JOIN account_groups g ON g.id = acc.group_id"
        f" {image_sql}"
        f" {where_sql}"
        f" {order_sql}"
        f" {limit_sql}"
    )
    params = select_params + params + [limit, offset]
    return query_sql, params


def _list_articles(
    storage: StorageLike,
    *,
    group_id: Optional[int],
    biz: Optional[str],
    query: Optional[str],
    since_ts: Optional[int],
    until_ts: Optional[int],
    content_only: bool,
    page: int,
    page_size: int,
    article_id: Optional[str] = None,
) -> dict[str, Any]:
    offset = max(page - 1, 0) * page_size
    query_sql, params = _build_article_query(
        storage=storage,
        group_id=group_id,
        biz=biz,
        query=query,
        since_ts=since_ts,
        until_ts=until_ts,
        content_only=content_only,
        limit=page_size,
        offset=offset,
        article_id=article_id,
    )
    rows = _fetchall(storage, query_sql, params)
    for row in rows:
        row["account_avatar_url"] = f"/api/account/{row['biz']}/avatar"

    where: list[str] = []
    count_params: list[Any] = []

    if article_id:
        where.append("a.article_id = %s")
        count_params.append(article_id)
        
    if group_id is not None:
        where.append("acc.group_id = %s")
        count_params.append(group_id)
    if biz:
        where.append("a.biz = %s")
        count_params.append(biz)
    if query:
        query_text = query.strip()
        if query_text:
            where.append("a.search_vector @@ plainto_tsquery('jiebaqry', %s)")
            count_params.append(query_text)
    if since_ts is not None:
        where.append("a.publish_at >= %s")
        count_params.append(since_ts)
    if until_ts is not None:
        where.append("a.publish_at <= %s")
        count_params.append(until_ts)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    count_sql = (
        "SELECT COUNT(*) AS total"
        " FROM articles a"
        " JOIN accounts acc ON acc.biz = a.biz"
        + (" JOIN article_content ac ON ac.article_pk = a.id" if content_only else "")
        + f" {where_sql}"
    )
    total_row = _fetchone(storage, count_sql, count_params)
    total = int(total_row["total"]) if total_row else 0
    return {"articles": rows, "page": page, "page_size": page_size, "total": total}


def _get_article(storage: StorageLike, article_id: int) -> dict[str, Any]:
    article = _fetchone(
        storage,
        (
            "SELECT a.id, a.biz, a.article_id, a.title, a.author, a.digest, a.cover, a.link,"
            " a.source_url, a.publish_at, a.created_at,"
            " acc.nickname AS account_nickname, acc.alias AS account_alias,"
            " acc.round_head_img AS account_avatar, acc.group_id, g.name AS group_name"
            " FROM articles a"
            " JOIN accounts acc ON acc.biz = a.biz"
            " LEFT JOIN account_groups g ON g.id = acc.group_id"
            " WHERE a.id = %s"
        ),
        [article_id],
    )
    if not article:
        raise ApiError("Article not found", status=404)
    article["account_avatar_url"] = f"/api/account/{article['biz']}/avatar"

    content_row = _fetchone(
        storage,
        "SELECT content_json, clean_html FROM article_content WHERE article_pk = %s",
        [article_id],
    )
    content_json = None
    clean_html = None
    if content_row:
        content_json = content_row.get("content_json")
        clean_html = content_row.get("clean_html")
        if isinstance(content_json, str):
            try:
                content_json = json.loads(content_json)
            except json.JSONDecodeError:
                content_json = None

    images = _fetchall(
        storage,
        "SELECT id, position, kind, content_type FROM article_images WHERE article_pk = %s ORDER BY position ASC",
        [article_id],
    )
    return {
        "article": article,
        "content": content_json,
        "images": images,
    }


def _list_article_images(storage: StorageLike, article_id: int) -> list[dict[str, Any]]:
    return _fetchall(
        storage,
        "SELECT id, position, kind, content_type FROM article_images WHERE article_pk = %s ORDER BY position ASC",
        [article_id],
    )


def _fetch_image(storage: StorageLike, image_id: int) -> tuple[bytes, str]:
    row = _fetchone(
        storage,
        "SELECT data, content_type FROM article_images WHERE id = %s",
        [image_id],
    )
    if not row:
        raise ApiError("Image not found", status=404)
    data = row.get("data")
    if data is None:
        raise ApiError("Image data missing", status=404)
    if isinstance(data, memoryview):
        payload = data.tobytes()
    else:
        payload = bytes(data)
    content_type = row.get("content_type") or "application/octet-stream"
    return payload, content_type


def _get_avatar_row(storage: StorageLike, biz: str) -> Optional[dict[str, Any]]:
    return _fetchone(
        storage,
        "SELECT avatar_url, content_type, data FROM avatar_images WHERE biz = %s",
        [biz],
    )


def _upsert_avatar_url(storage: StorageLike, biz: str, url: str) -> None:
    with storage.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO avatar_images (biz, avatar_url, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (biz) DO UPDATE SET
                avatar_url=EXCLUDED.avatar_url,
                updated_at=EXCLUDED.updated_at
            """,
            (biz, url, _utc_now_iso()),
        )
    storage.conn.commit()


def _store_avatar(
    storage: StorageLike,
    biz: str,
    *,
    content_type: str,
    data: bytes,
    avatar_url: Optional[str] = None,
) -> None:
    with storage.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO avatar_images (biz, avatar_url, content_type, data, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (biz) DO UPDATE SET
                avatar_url=COALESCE(EXCLUDED.avatar_url, avatar_images.avatar_url),
                content_type=EXCLUDED.content_type,
                data=EXCLUDED.data,
                updated_at=EXCLUDED.updated_at
            """,
            (biz, avatar_url, content_type, psycopg2.Binary(data), _utc_now_iso()),
        )
    storage.conn.commit()


def _fetch_and_cache_avatar(storage: StorageLike, biz: str, url: str) -> Optional[tuple[bytes, str]]:
    headers = {
        "Referer": "https://mp.weixin.qq.com/",
        "Origin": "https://mp.weixin.qq.com",
        "User-Agent": "Mozilla/5.0",
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None
        content_type = resp.headers.get("Content-Type") or "application/octet-stream"
        data = resp.content
        if data:
            _store_avatar(storage, biz, content_type=content_type, data=data, avatar_url=url)
        return data, content_type
    except Exception as exc:
        logger.warning("Failed to fetch avatar for %s: %s", biz, exc)
        return None


def _list_feed(
    storage: StorageLike,
    *,
    group_id: Optional[int],
    biz: Optional[str],
    query: Optional[str],
    since_ts: Optional[int],
    until_ts: Optional[int],
    limit: int,
) -> list[dict[str, Any]]:
    query_sql, params = _build_article_query(
        storage=storage,
        group_id=group_id,
        biz=biz,
        query=query,
        since_ts=since_ts,
        until_ts=until_ts,
        content_only=False,
        limit=limit,
        offset=0,
    )
    return _fetchall(storage, query_sql, params)




def _binary_response(payload: bytes, content_type: str) -> Response:
    return Response(
        content=payload,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=259200"},
    )


def _get_storage() -> Generator[StorageLike, None, None]:
    with open_storage() as storage:
        _ensure_default_group(storage)
        _ensure_avatar_images_table(storage)
        yield storage


def _get_login_manager(request: Request) -> LoginManager:
    return request.app.state.login_manager


def _get_sync_scheduler(request: Request) -> SyncScheduler:
    return request.app.state.sync_scheduler


router = APIRouter(prefix="/api")


@router.get("/group")
def list_groups(storage: StorageLike = Depends(_get_storage)) -> dict[str, Any]:
    default_group = _ensure_default_group(storage)
    return {
        "default_group_id": default_group["id"],
        "groups": _list_groups(storage),
    }


@router.post("/group", status_code=status.HTTP_201_CREATED)
def create_group(
    body: dict[str, Any] = Body(default={}),
    storage: StorageLike = Depends(_get_storage),
) -> dict[str, Any]:
    name = str(body.get("name", "")).strip()
    if not name:
        raise ApiError("Group name is required")
    group = storage.upsert_group(name)
    return {"id": group.id, "name": group.name}


@router.get("/group/{group_id}")
def get_group(
    group_id: int,
    storage: StorageLike = Depends(_get_storage),
) -> dict[str, Any]:
    return _get_group(storage, group_id)


@router.patch("/group/{group_id}")
def update_group(
    group_id: int,
    body: dict[str, Any] = Body(default={}),
    storage: StorageLike = Depends(_get_storage),
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if 'name' in body:
        name = str(body.get('name', '')).strip()
        if not name:
            raise ApiError('Group name is required')
        updates['name'] = name
    if 'sync_mode' in body:
        updates['sync_mode'] = _normalize_sync_mode(body.get('sync_mode'))
    if 'sync_recent_days' in body:
        updates['sync_recent_days'] = _normalize_recent_days(body.get('sync_recent_days'))
    return _update_group(storage, group_id, updates)


@router.delete("/group/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group(
    group_id: int,
    storage: StorageLike = Depends(_get_storage),
) -> Response:
    _delete_group(storage, group_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/account/search")
async def search_account(
    q: str = "",
    page: int = 1,
    page_size: int = 10,
    begin: Optional[int] = None,
    storage: StorageLike = Depends(_get_storage),
) -> dict[str, Any]:
    keyword = (q or "").strip()
    if not keyword:
        raise ApiError("q is required")
    offset = begin if begin is not None else (max(page, 1) - 1) * page_size
    _ensure_avatar_images_table(storage)
    existing = {account.biz for account in storage.list_accounts()}
    session = storage.get_login_session()
    async with MPClient() as client:
        payload = await client.search_biz(
            session,
            keyword=keyword,
            begin=offset,
            count=min(max(page_size, 1), 20),
        )
    records = payload.get("list") or []
    results: list[dict[str, Any]] = []
    for item in records:
        biz = (item.get("fakeid") or "").strip()
        if not biz:
            continue
        avatar_url = (item.get("round_head_img") or item.get("headimg") or "").strip()
        if avatar_url:
            _upsert_avatar_url(storage, biz, avatar_url)
        is_added = biz in existing
        results.append(
            {
                "biz": biz,
                "nickname": item.get("nickname") or "",
                "alias": item.get("alias") or "",
                "round_head_img": avatar_url,
                "is_added": is_added,
                "avatar_url": (
                    f"/api/account/{biz}/avatar"
                    if is_added
                    else f"/api/account/search/{biz}/avatar"
                ),
            }
        )
    return {
        "results": results,
        "page": max(page, 1),
        "page_size": min(max(page_size, 1), 20),
        "total": payload.get("total") or len(results),
    }


@router.get("/account/search/{biz}/avatar")
def get_search_avatar(
    biz: str,
    storage: StorageLike = Depends(_get_storage),
) -> Response:
    _ensure_avatar_images_table(storage)
    avatar = _get_avatar_row(storage, biz)
    if not avatar:
        raise ApiError("Avatar not found", status=404)
    data = avatar.get("data")
    if not data:
        url = avatar.get("avatar_url")
        if url:
            cached = _fetch_and_cache_avatar(storage, biz, url)
            if cached:
                payload, content_type = cached
                return _binary_response(payload, content_type)
        raise ApiError("Avatar not found", status=404)
    payload = data.tobytes() if isinstance(data, memoryview) else bytes(data)
    content_type = avatar.get("content_type") or "application/octet-stream"
    return _binary_response(payload, content_type)


@router.get("/account")
def list_accounts(
    group_id: Optional[int] = None,
    q: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    storage: StorageLike = Depends(_get_storage),
) -> dict[str, Any]:
    payload = _list_accounts(
        storage,
        group_id=group_id,
        query=q or None,
        page=max(page, 1),
        page_size=min(max(page_size, 1), 200),
    )
    return payload


@router.post("/account", status_code=status.HTTP_201_CREATED)
def create_account(
    body: dict[str, Any] = Body(default={}),
    storage: StorageLike = Depends(_get_storage),
) -> dict[str, Any]:
    required = ["biz", "nickname"]
    for field in required:
        if not body.get(field):
            raise ApiError(f"{field} is required")
    group_id = body.get("group_id")
    if group_id is None:
        default_group = _ensure_default_group(storage)
        group_id = default_group["id"]
    sync_mode = _normalize_sync_mode(body.get('sync_mode'))
    sync_recent_days = _normalize_recent_days(body.get('sync_recent_days'))
    account = storage.upsert_account(
        AccountCredential(
            biz=str(body["biz"]),
            nickname=str(body["nickname"]),
            alias=body.get("alias"),
            round_head_img=body.get("round_head_img"),
            group_id=int(group_id) if group_id is not None else None,
            sync_mode=sync_mode,
            sync_recent_days=sync_recent_days,
        )
    )
    return {
        "biz": account.biz,
        "nickname": account.nickname,
        "alias": account.alias,
        "round_head_img": account.round_head_img,
        "group_id": account.group_id,
    }


@router.post("/account/move")
def move_accounts(
    body: dict[str, Any] = Body(default={}),
    storage: StorageLike = Depends(_get_storage),
) -> dict[str, Any]:
    biz_list = body.get("biz_list") or []
    if not isinstance(biz_list, list) or not biz_list:
        raise ApiError("biz_list is required")
    group_id = body.get("group_id")
    if group_id is None:
        default_group = _ensure_default_group(storage)
        group_id = default_group["id"]
    with storage.conn.cursor() as cur:
        cur.execute(
            "UPDATE accounts SET group_id = %s, updated_at = NOW() WHERE biz = ANY(%s)",
            (group_id, biz_list),
        )
    storage.conn.commit()
    return {"updated": len(biz_list)}


@router.post("/account/batch")
def batch_update_accounts(
    body: dict[str, Any] = Body(default={}),
    storage: StorageLike = Depends(_get_storage),
) -> dict[str, Any]:
    biz_list = body.get('biz_list') or []
    if not isinstance(biz_list, list) or not biz_list:
        raise ApiError('biz_list is required')
    updates: dict[str, Any] = {}
    if 'sync_mode' in body:
        updates['sync_mode'] = _normalize_sync_mode(body.get('sync_mode'))
    if 'sync_recent_days' in body:
        updates['sync_recent_days'] = _normalize_recent_days(body.get('sync_recent_days'))
    if not updates:
        raise ApiError('No fields to update')
    fields: list[str] = []
    params: list[Any] = []
    mapping = {
        'sync_mode': 'sync_mode',
        'sync_recent_days': 'sync_recent_days',
    }
    for key, column in mapping.items():
        if key in updates:
            fields.append(f"{column} = %s")
            params.append(updates[key])
    fields.append('updated_at = NOW()')
    params.append(biz_list)
    with storage.conn.cursor() as cur:
        cur.execute(
            f"UPDATE accounts SET {', '.join(fields)} WHERE biz = ANY(%s)",
            params,
        )
        updated = cur.rowcount
    storage.conn.commit()
    return {'updated': updated}


@router.get("/account/{biz}")
def get_account(
    biz: str,
    storage: StorageLike = Depends(_get_storage),
) -> dict[str, Any]:
    return _get_account(storage, biz)


@router.patch("/account/{biz}")
def update_account(
    biz: str,
    body: dict[str, Any] = Body(default={}),
    storage: StorageLike = Depends(_get_storage),
) -> dict[str, Any]:
    if "group_id" in body and body["group_id"] is None:
        default_group = _ensure_default_group(storage)
        body["group_id"] = default_group["id"]
    return _update_account(storage, biz, body)


@router.delete("/account/{biz}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    biz: str,
    storage: StorageLike = Depends(_get_storage),
) -> Response:
    removed = storage.remove_account(biz)
    if removed == 0:
        raise ApiError("Account not found", status=404)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/account/{biz}/avatar")
def get_account_avatar(
    biz: str,
    storage: StorageLike = Depends(_get_storage),
) -> Response:
    avatar = _get_avatar_row(storage, biz)
    data = avatar.get("data") if avatar else None
    if not data:
        url = avatar.get("avatar_url") if avatar else None
        if not url:
            row = _fetchone(
                storage,
                "SELECT round_head_img FROM accounts WHERE biz = %s",
                [biz],
            )
            if not row:
                raise ApiError("Account not found", status=404)
            url = row.get("round_head_img")
            if url:
                _upsert_avatar_url(storage, biz, url)
        if url:
            cached = _fetch_and_cache_avatar(storage, biz, url)
            if cached:
                payload, content_type = cached
                return _binary_response(payload, content_type)
        raise ApiError("Avatar not found", status=404)
    payload = data.tobytes() if isinstance(data, memoryview) else bytes(data)
    content_type = avatar.get("content_type") or "application/octet-stream"
    return _binary_response(payload, content_type)


@router.get("/article")
def list_articles(
    group_id: Optional[int] = None,
    biz: Optional[str] = None,
    article_id: Optional[str] = None,
    q: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    content: str = "",
    since: Optional[str] = None,
    until: Optional[str] = None,
    storage: StorageLike = Depends(_get_storage),
) -> dict[str, Any]:
    content_only = (content or "").lower() in {"1", "true", "yes"}
    since_ts = _parse_date(since)
    until_ts = _parse_date(until, end_of_day=True)
    return _list_articles(
        storage,
        group_id=group_id,
        biz=biz or None,
        query=q or None,
        since_ts=since_ts,
        until_ts=until_ts,
        content_only=content_only,
        page=max(page, 1),
        page_size=min(max(page_size, 1), 200),
        article_id=article_id or None,
    )


@router.get("/article/{article_id}")
def get_article(
    article_id: int,
    storage: StorageLike = Depends(_get_storage),
) -> dict[str, Any]:
    return _get_article(storage, article_id)


@router.get("/article/{article_id}/image")
def list_article_images(
    article_id: int,
    storage: StorageLike = Depends(_get_storage),
) -> dict[str, Any]:
    payload = _list_article_images(storage, article_id)
    return {"images": payload}


@router.get("/image/{image_id}")
def get_image(
    image_id: int,
    storage: StorageLike = Depends(_get_storage),
) -> Response:
    payload, content_type = _fetch_image(storage, image_id)
    return _binary_response(payload, content_type)


@router.get("/login")
def login_status(
    storage: StorageLike = Depends(_get_storage),
    manager: "LoginManager" = Depends(_get_login_manager),
) -> dict[str, Any]:
    info = _get_login_info(storage)
    snapshot = manager._snapshot()
    return {
        **snapshot,
        "qrcode_url": "/api/login/qrcode" if snapshot.get("has_qrcode") else None,
        "last_login": info,
    }


@router.post("/login/start")
async def login_start(
    storage: StorageLike = Depends(_get_storage),
    manager: "LoginManager" = Depends(_get_login_manager),
) -> dict[str, Any]:
    snapshot = await manager.start()
    info = _get_login_info(storage)
    return {
        **snapshot,
        "qrcode_url": "/api/login/qrcode" if snapshot.get("has_qrcode") else None,
        "last_login": info,
    }


@router.post("/login/poll")
async def login_poll(
    storage: StorageLike = Depends(_get_storage),
    manager: "LoginManager" = Depends(_get_login_manager),
) -> dict[str, Any]:
    snapshot = await manager.poll(storage)
    info = _get_login_info(storage)
    return {
        **snapshot,
        "qrcode_url": "/api/login/qrcode" if snapshot.get("has_qrcode") else None,
        "last_login": info,
    }


@router.post("/login/cancel")
def login_cancel(
    storage: StorageLike = Depends(_get_storage),
    manager: "LoginManager" = Depends(_get_login_manager),
) -> dict[str, Any]:
    manager.cancel()
    info = _get_login_info(storage)
    snapshot = manager._snapshot()
    return {
        **snapshot,
        "qrcode_url": "/api/login/qrcode" if snapshot.get("has_qrcode") else None,
        "last_login": info,
    }


@router.get("/login/qrcode")
def login_qrcode(
    manager: "LoginManager" = Depends(_get_login_manager),
) -> Response:
    data = manager.get_qrcode()
    return Response(content=data, media_type="image/png")


@router.get("/sync")
def sync_status(storage: StorageLike = Depends(_get_storage)) -> dict[str, Any]:
    return _get_sync_status(storage)


@router.get("/sync/settings")
def get_sync_settings(storage: StorageLike = Depends(_get_storage)) -> dict[str, Any]:
    payload = _get_sync_settings(storage)
    payload["email"] = _get_email_settings(storage)
    return payload


@router.patch("/sync/settings")
def update_sync_settings(
    body: dict[str, Any] = Body(default={}),
    storage: StorageLike = Depends(_get_storage),
    scheduler: "SyncScheduler" = Depends(_get_sync_scheduler),
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if "enabled" in body:
        updates["enabled"] = bool(body["enabled"])
    if "interval_minutes" in body:
        updates["interval_minutes"] = max(int(body["interval_minutes"]), 1)
    if "mode" in body:
        updates["mode"] = body["mode"]
    if "recent_days" in body:
        updates["recent_days"] = max(int(body["recent_days"]), 1)
    if "page_size" in body:
        updates["page_size"] = max(int(body["page_size"]), 1)
    if "page_limit" in body:
        value = body["page_limit"]
        if value in ("", None):
            updates["page_limit"] = None
        else:
            updates["page_limit"] = max(int(value), 1)
    if "sleep_seconds" in body:
        updates["sleep_seconds"] = float(body["sleep_seconds"])
    if "download_content" in body:
        updates["download_content"] = bool(body["download_content"])
    if "download_images" in body:
        updates["download_images"] = bool(body["download_images"])
    if "content_limit" in body:
        updates["content_limit"] = max(int(body["content_limit"]), 0)
    if "skip_minutes" in body:
        updates["skip_minutes"] = max(int(body["skip_minutes"]), 0)
    if "since" in body:
        updates["since"] = body["since"]
    if "until" in body:
        updates["until"] = body["until"]
    if "alert_enabled" in body:
        updates["alert_enabled"] = bool(body["alert_enabled"])
    if "alert_email" in body:
        updates["alert_email"] = str(body["alert_email"]).strip()
    settings = _set_sync_settings(storage, updates)
    email_updates: dict[str, Any] = {}
    email_body = body.get("email")
    if isinstance(email_body, dict):
        for key in (
            "smtp_host",
            "smtp_port",
            "smtp_user",
            "smtp_password",
            "smtp_tls",
            "from_email",
        ):
            if key in email_body:
                email_updates[key] = email_body[key]
    if email_updates:
        settings["email"] = _set_email_settings(storage, email_updates)
    else:
        settings["email"] = _get_email_settings(storage)
    if settings.get("enabled"):
        scheduler.trigger()
    return settings


@router.post("/sync/run", status_code=status.HTTP_202_ACCEPTED)
def run_sync(
    body: dict[str, Any] = Body(default={}),
    storage: StorageLike = Depends(_get_storage),
    scheduler: "SyncScheduler" = Depends(_get_sync_scheduler),
) -> dict[str, Any]:
    group_id = body.get('group_id')
    if group_id is not None:
        try:
            group_id = int(group_id)
        except (TypeError, ValueError) as exc:
            raise ApiError('Invalid group_id') from exc
        row = _fetchone(
            storage,
            'SELECT id FROM account_groups WHERE id = %s',
            [group_id],
        )
        if not row:
            raise ApiError('Group not found', status=404)
        threading.Thread(target=scheduler.run_group, args=(group_id,), daemon=True).start()
        return {'status': 'running', 'group_id': group_id}
    threading.Thread(target=scheduler.run_once, daemon=True).start()
    return {'status': 'running'}


@router.get("/feed/mixed", response_model=None)
def list_feed(
    request: Request,
    group_id: Optional[int] = None,
    biz: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 50,
    format: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    days: Optional[int] = None,
    storage: StorageLike = Depends(_get_storage),
):
    output_format = (format or "").lower()
    since_ts = _parse_date(since)
    until_ts = _parse_date(until, end_of_day=True)
    if days:
        now = datetime.utcnow()
        since_ts = int((now.timestamp() - days * 86400))
    if output_format == "rss":
        group_names: list[str] = []
        if group_id is not None:
            row = _fetchone(
                storage,
                "SELECT name FROM account_groups WHERE id = %s",
                [group_id],
            )
            if not row:
                raise ApiError("Group not found", status=404)
            group_names = [row.get("name") or ""]
        host = request.headers.get("host") or f"{DEFAULT_HOST}:{DEFAULT_PORT}"
        scheme = request.url.scheme or "http"
        image_base = f"{scheme}://{host}"
        items = query_rss_items(
            group_names=group_names,
            limit=min(max(limit, 1), 500),
            days=days,
            since=since,
            until=until,
            image_base_url=image_base,
        )
        title = "Hippo RSS"
        description = "Hippo RSS feed"
        if group_names:
            title = f"{group_names[0]} - Hippo RSS"
            description = f"RSS feed for {group_names[0]}"
        xml = build_rss_xml(
            title=title,
            link=image_base,
            description=description,
            items=items,
        )
        return Response(
            content=xml.encode("utf-8"),
            media_type="application/rss+xml; charset=utf-8",
        )
    payload = _list_feed(
        storage,
        group_id=group_id,
        biz=biz or None,
        query=q or None,
        since_ts=since_ts,
        until_ts=until_ts,
        limit=min(max(limit, 1), 500),
    )
    return {"articles": payload}


def create_app(static_dir: Path | str = "static") -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    static_path = Path(static_dir).expanduser().resolve()
    if not static_path.exists():
        raise RuntimeError(f"Static directory not found: {static_path}")

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    def _startup() -> None:
        app.state.login_manager = LoginManager()
        app.state.sync_scheduler = SyncScheduler()
        app.state.sync_scheduler.start()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        scheduler = getattr(app.state, "sync_scheduler", None)
        if scheduler:
            scheduler.stop()

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"error": str(exc)})

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("API error")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": str(exc)},
        )

    app.include_router(router)
    app.mount("/", StaticFiles(directory=static_path, html=True), name="static")
    return app


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    static_dir: Path | str = "static",
) -> None:
    import uvicorn

    app = create_app(static_dir=static_dir)
    uvicorn.run(app, host=host, port=port, log_level="info")
