"""Minimal HTTP server for Hippo API + static UI."""

from __future__ import annotations

import json
import logging
import mimetypes
import random
import threading
import time as time_module
from datetime import date, datetime, time, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import psycopg2
import psycopg2.extras

from .config import DB_PATH
from .downloader import ArticleDownloader
from .http import MPClient, parse_appmsg_publish
from .models import AccountCredential, ArticleRecord
from .storage import PostgresStorage, StorageLike, open_storage

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_GROUP_NAME = "Default"
SYNC_STATUS_KEY = "sync:last_status"
SYNC_ERROR_KEY = "sync:last_error"
SYNC_STARTED_KEY = "sync:last_started_at"
SYNC_FINISHED_KEY = "sync:last_finished_at"
SYNC_HISTORY_KEY = "sync:history"
SYNC_SETTINGS_KEY = "sync:settings"

logger = logging.getLogger("hippo.serve")


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _is_postgres(storage: StorageLike) -> bool:
    return isinstance(storage, PostgresStorage)


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(f"Invalid integer: {value}") from exc


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
    }


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
        "SELECT nickname, avatar, updated_at FROM login_sessions ORDER BY id DESC LIMIT 1"
        if _is_postgres(storage)
        else "SELECT nickname, avatar, updated_at FROM login_sessions ORDER BY id DESC LIMIT 1",
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

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._status in ("starting", "waiting", "scanned", "refresh"):
                return self._snapshot()
            self._status = "starting"
            self._message = "Requesting QR code"
        sid = f"{int(time_module.time() * 1000)}{random.randint(100, 999)}"
        try:
            with MPClient(timeout=15.0) as client:
                uuid_cookie = client.start_login_session(sid)
                qrcode_bytes = client.fetch_login_qrcode(uuid_cookie)
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

    def poll(self, storage: StorageLike) -> dict[str, Any]:
        with self._lock:
            uuid_cookie = self._uuid_cookie
        if not uuid_cookie:
            raise ApiError("Login not started", status=400)
        try:
            with MPClient(timeout=15.0) as client:
                resp = client.check_login_status(uuid_cookie)
                if resp.get("base_resp", {}).get("ret") != 0:
                    raise ApiError("Login status error")
                status = resp.get("status")
                if status == 1:
                    session = client.finalize_login(uuid_cookie)
                    info = client.fetch_login_info(session)
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
                    qrcode_bytes = client.fetch_login_qrcode(uuid_cookie)
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
            with open_storage(DB_PATH) as storage:
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
            return {"status": "running"}
        try:
            return self._run_sync()
        finally:
            self._lock.release()

    def _run_sync(self) -> dict[str, Any]:
        started_at = _utc_now_iso()
        with open_storage(DB_PATH) as storage:
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
            total_saved = 0
            total_downloaded = 0
            skipped_accounts = 0
            error: Optional[str] = None
            with MPClient() as client:
                downloader = ArticleDownloader(
                    client=client,
                    storage=storage,
                    write_local=False,
                    enable_image_worker=bool(settings.get("download_images")),
                )
                with downloader:
                    for account in accounts:
                        if account.is_disabled:
                            skipped_accounts += 1
                            continue
                        if _should_skip_by_time(account.last_synced_at, settings.get("skip_minutes")):
                            skipped_accounts += 1
                            continue
                        try:
                            saved, downloaded = _sync_account_articles(
                                storage=storage,
                                client=client,
                                downloader=downloader,
                                account=account,
                                settings=settings,
                            )
                        except Exception as exc:
                            message = str(exc)
                            if _is_login_error(message):
                                error = message
                                break
                            if _is_freq_control(message):
                                time_module.sleep(15)
                                continue
                            error = message
                            break
                        total_saved += saved
                        total_downloaded += downloaded
                    if settings.get("download_images"):
                        downloader.wait_for_images()

            finished_at = _utc_now_iso()
            if error:
                status = "login_required" if _is_login_error(error) else "failed"
                _set_sync_state(storage, status=status, error=error, finished_at=finished_at)
            else:
                _set_sync_state(storage, status="success", error="", finished_at=finished_at)
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
            return _get_sync_status(storage)


def _fetchall(storage: StorageLike, query: str, params: list[Any]) -> list[dict[str, Any]]:
    if _is_postgres(storage):
        with storage.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return [_normalize_record(dict(row)) for row in rows]
    cur = storage.conn.execute(query, params)
    rows = cur.fetchall()
    return [_normalize_record(dict(row)) for row in rows]


def _fetchone(storage: StorageLike, query: str, params: list[Any]) -> Optional[dict[str, Any]]:
    if _is_postgres(storage):
        with storage.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            row = cur.fetchone()
        return _normalize_record(dict(row)) if row else None
    row = storage.conn.execute(query, params).fetchone()
    return _normalize_record(dict(row)) if row else None


def _ensure_default_group(storage: StorageLike) -> dict[str, Any]:
    groups = storage.list_groups()
    default_group = next((g for g in groups if g.name == DEFAULT_GROUP_NAME), None)
    if default_group is None:
        default_group = storage.upsert_group(DEFAULT_GROUP_NAME)
    default_id = default_group.id
    if _is_postgres(storage):
        with storage.conn.cursor() as cur:
            cur.execute(
                "UPDATE accounts SET group_id = %s WHERE group_id IS NULL",
                (default_id,),
            )
        storage.conn.commit()
    else:
        storage.conn.execute(
            "UPDATE accounts SET group_id = ? WHERE group_id IS NULL",
            (default_id,),
        )
        storage.conn.commit()
    return {"id": default_id, "name": default_group.name}


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


def _sync_account_articles(
    *,
    storage: StorageLike,
    client: MPClient,
    downloader: ArticleDownloader,
    account: AccountCredential,
    settings: dict[str, Any],
) -> tuple[int, int]:
    page_size = max(int(settings.get("page_size") or 10), 1)
    page_limit = settings.get("page_limit")
    if page_limit is not None:
        page_limit = max(int(page_limit), 1)
    mode = settings.get("mode") or "incremental"
    now = datetime.now(timezone.utc)
    since_ts: Optional[int] = None
    until_ts: Optional[int] = None
    stop_on_existing = False
    if mode == "incremental":
        stop_on_existing = True
        if account.last_synced_at:
            since_ts = int(account.last_synced_at.timestamp())
    elif mode == "recent":
        recent_days = max(int(settings.get("recent_days") or 1), 1)
        since_ts = int((now.timestamp() - recent_days * 86400))
    elif mode == "range":
        since_ts = _parse_date(settings.get("since"))
        until_ts = _parse_date(settings.get("until"), end_of_day=True)

    session = storage.get_login_session()
    offset = 0
    page_count = 0
    total_saved = 0
    to_download: list[ArticleRecord] = []

    while True:
        payload = client.fetch_appmsg_publish(
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
            time_module.sleep(sleep_seconds)

    downloaded = 0
    if settings.get("download_content"):
        content_limit = int(settings.get("content_limit") or 0)
        candidates = {item.article_id: item for item in to_download}
        for missing in _select_missing_content(storage, account.biz, limit=content_limit):
            candidates.setdefault(missing.article_id, missing)
        if candidates:
            results, _, _ = downloader.download_many(
                candidates.values(),
                fmt="html",
                with_images=bool(settings.get("download_images")),
                record_images_only=not bool(settings.get("download_images")),
                account_name=account.nickname or account.biz,
                skip_if_downloaded=True,
            )
            downloaded = len(results)
    return total_saved, downloaded


def _list_groups(storage: StorageLike) -> list[dict[str, Any]]:
    return [
        {"id": g.id, "name": g.name, "account_count": g.account_count}
        for g in storage.list_groups()
    ]


def _get_group(storage: StorageLike, group_id: int) -> dict[str, Any]:
    row = _fetchone(
        storage,
        """
        SELECT g.id, g.name, COUNT(a.biz) AS account_count
        FROM account_groups g
        LEFT JOIN accounts a ON a.group_id = g.id
        WHERE g.id = %s
        GROUP BY g.id, g.name
        """ if _is_postgres(storage) else """
        SELECT g.id, g.name, COUNT(a.biz) AS account_count
        FROM account_groups g
        LEFT JOIN accounts a ON a.group_id = g.id
        WHERE g.id = ?
        GROUP BY g.id, g.name
        """,
        [group_id],
    )
    if not row:
        raise ApiError("Group not found", status=404)
    return row


def _update_group(storage: StorageLike, group_id: int, name: str) -> dict[str, Any]:
    trimmed = name.strip()
    if not trimmed:
        raise ApiError("Group name cannot be empty")
    if _is_postgres(storage):
        with storage.conn.cursor() as cur:
            cur.execute(
                "UPDATE account_groups SET name = %s, updated_at = NOW() WHERE id = %s",
                (trimmed, group_id),
            )
            updated = cur.rowcount
        storage.conn.commit()
    else:
        updated = storage.conn.execute(
            "UPDATE account_groups SET name = ?, updated_at = ? WHERE id = ?",
            (trimmed, datetime.utcnow().isoformat(), group_id),
        ).rowcount
        storage.conn.commit()
    if updated == 0:
        raise ApiError("Group not found", status=404)
    return _get_group(storage, group_id)


def _delete_group(storage: StorageLike, group_id: int) -> None:
    default_group = _ensure_default_group(storage)
    default_id = default_group["id"]
    if group_id == default_id:
        raise ApiError("Default group cannot be deleted", status=400)
    if _is_postgres(storage):
        with storage.conn.cursor() as cur:
            cur.execute(
                "UPDATE accounts SET group_id = %s WHERE group_id = %s",
                (default_id, group_id),
            )
            cur.execute("DELETE FROM account_groups WHERE id = %s", (group_id,))
            deleted = cur.rowcount
        storage.conn.commit()
    else:
        storage.conn.execute(
            "UPDATE accounts SET group_id = ? WHERE group_id = ?",
            (default_id, group_id),
        )
        deleted = storage.conn.execute(
            "DELETE FROM account_groups WHERE id = ?",
            (group_id,),
        ).rowcount
        storage.conn.commit()
    if deleted == 0:
        raise ApiError("Group not found", status=404)


def _build_search_clause(
    *,
    is_postgres: bool,
    term: str,
    fields: list[str],
) -> tuple[str, list[Any]]:
    like = f"%{term}%"
    if is_postgres:
        clause = " OR ".join([f"{field} ILIKE %s" for field in fields])
    else:
        clause = " OR ".join([f"{field} LIKE ? COLLATE NOCASE" for field in fields])
    return f"({clause})", [like for _ in fields]


def _list_accounts(
    storage: StorageLike,
    *,
    group_id: Optional[int],
    query: Optional[str],
    page: int,
    page_size: int,
) -> dict[str, Any]:
    is_pg = _is_postgres(storage)
    where: list[str] = []
    params: list[Any] = []
    if group_id is not None:
        where.append("a.group_id = %s" if is_pg else "a.group_id = ?")
        params.append(group_id)
    if query:
        clause, values = _build_search_clause(
            is_postgres=is_pg,
            term=query,
            fields=["a.nickname", "a.alias", "a.biz"],
        )
        where.append(clause)
        params.extend(values)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    limit_sql = "LIMIT %s OFFSET %s" if is_pg else "LIMIT ? OFFSET ?"
    offset = max(page - 1, 0) * page_size
    query_sql = (
        ""
        "SELECT a.biz, a.nickname, a.alias, a.round_head_img, a.group_id, a.is_default,"
        " a.is_disabled, a.last_synced_at, g.name AS group_name"
        " FROM accounts a"
        " LEFT JOIN account_groups g ON g.id = a.group_id"
        f" {where_sql}"
        " ORDER BY a.is_default DESC, a.nickname ASC"
        f" {limit_sql}"
    )
    rows = _fetchall(storage, query_sql, params + [page_size, offset])
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
            "SELECT a.biz, a.nickname, a.alias, a.round_head_img, a.group_id, a.is_default,"
            " a.is_disabled, a.last_synced_at, g.name AS group_name"
            " FROM accounts a"
            " LEFT JOIN account_groups g ON g.id = a.group_id"
            " WHERE a.biz = %s"
        )
        if _is_postgres(storage)
        else (
            ""
            "SELECT a.biz, a.nickname, a.alias, a.round_head_img, a.group_id, a.is_default,"
            " a.is_disabled, a.last_synced_at, g.name AS group_name"
            " FROM accounts a"
            " LEFT JOIN account_groups g ON g.id = a.group_id"
            " WHERE a.biz = ?"
        ),
        [biz],
    )
    if not row:
        raise ApiError("Account not found", status=404)
    return row


def _update_account(storage: StorageLike, biz: str, payload: dict[str, Any]) -> dict[str, Any]:
    fields: list[str] = []
    params: list[Any] = []
    is_pg = _is_postgres(storage)

    mapping = {
        "nickname": "nickname",
        "alias": "alias",
        "round_head_img": "round_head_img",
        "group_id": "group_id",
        "is_default": "is_default",
        "is_disabled": "is_disabled",
    }

    for key, column in mapping.items():
        if key in payload:
            value = payload[key]
            if key in ("is_default", "is_disabled"):
                value = bool(value)
            fields.append(f"{column} = %s" if is_pg else f"{column} = ?")
            params.append(value)

    if not fields:
        raise ApiError("No fields to update")

    fields.append("updated_at = NOW()" if is_pg else "updated_at = ?")
    if not is_pg:
        params.append(datetime.utcnow().isoformat())

    params.append(biz)

    query = (
        f"UPDATE accounts SET {', '.join(fields)} WHERE biz = %s"
        if is_pg
        else f"UPDATE accounts SET {', '.join(fields)} WHERE biz = ?"
    )
    if is_pg:
        with storage.conn.cursor() as cur:
            cur.execute(query, params)
            updated = cur.rowcount
        storage.conn.commit()
    else:
        updated = storage.conn.execute(query, params).rowcount
        storage.conn.commit()
    if updated == 0:
        raise ApiError("Account not found", status=404)
    if payload.get("is_default"):
        storage.set_default_account(biz)
    return _get_account(storage, biz)


def _build_article_query(
    *,
    storage: StorageLike,
    group_id: Optional[int],
    biz: Optional[str],
    query: Optional[str],
    since_ts: Optional[int],
    until_ts: Optional[int],
    limit: int,
    offset: int,
) -> tuple[str, list[Any]]:
    is_pg = _is_postgres(storage)
    where: list[str] = []
    params: list[Any] = []

    if group_id is not None:
        where.append("acc.group_id = %s" if is_pg else "acc.group_id = ?")
        params.append(group_id)
    if biz:
        where.append("a.biz = %s" if is_pg else "a.biz = ?")
        params.append(biz)
    if query:
        clause, values = _build_search_clause(
            is_postgres=is_pg,
            term=query,
            fields=["a.title", "a.author", "a.digest"],
        )
        where.append(clause)
        params.extend(values)
    if since_ts is not None:
        where.append("a.publish_at >= %s" if is_pg else "a.publish_at >= ?")
        params.append(since_ts)
    if until_ts is not None:
        where.append("a.publish_at <= %s" if is_pg else "a.publish_at <= ?")
        params.append(until_ts)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    limit_sql = "LIMIT %s OFFSET %s" if is_pg else "LIMIT ? OFFSET ?"

    image_sql = (
        "LEFT JOIN LATERAL ("
        "  SELECT id FROM article_images i"
        "  WHERE i.article_pk = a.id AND i.data IS NOT NULL"
        "  ORDER BY i.position ASC"
        "  LIMIT 1"
        ") img ON TRUE"
        if is_pg
        else ""
    )
    image_select = "img.id AS image_id" if is_pg else (
        "(SELECT id FROM article_images i"
        " WHERE i.article_pk = a.id AND i.data IS NOT NULL"
        " ORDER BY i.position ASC"
        " LIMIT 1) AS image_id"
    )

    query_sql = (
        "SELECT a.id, a.biz, a.article_id, a.title, a.author, a.digest, a.cover, a.link,"
        " a.source_url, a.publish_at,"
        " acc.nickname AS account_nickname, acc.alias AS account_alias,"
        " acc.round_head_img AS account_avatar,"
        " acc.group_id, g.name AS group_name,"
        f" {image_select}"
        " FROM articles a"
        " JOIN accounts acc ON acc.biz = a.biz"
        " LEFT JOIN account_groups g ON g.id = acc.group_id"
        f" {image_sql}"
        f" {where_sql}"
        " ORDER BY a.publish_at IS NULL, a.publish_at DESC, a.id DESC"
        f" {limit_sql}"
    )
    params = params + [limit, offset]
    return query_sql, params


def _list_articles(
    storage: StorageLike,
    *,
    group_id: Optional[int],
    biz: Optional[str],
    query: Optional[str],
    since_ts: Optional[int],
    until_ts: Optional[int],
    page: int,
    page_size: int,
) -> dict[str, Any]:
    offset = max(page - 1, 0) * page_size
    query_sql, params = _build_article_query(
        storage=storage,
        group_id=group_id,
        biz=biz,
        query=query,
        since_ts=since_ts,
        until_ts=until_ts,
        limit=page_size,
        offset=offset,
    )
    rows = _fetchall(storage, query_sql, params)

    is_pg = _is_postgres(storage)
    where = []
    count_params: list[Any] = []
    if group_id is not None:
        where.append("acc.group_id = %s" if is_pg else "acc.group_id = ?")
        count_params.append(group_id)
    if biz:
        where.append("a.biz = %s" if is_pg else "a.biz = ?")
        count_params.append(biz)
    if query:
        clause, values = _build_search_clause(
            is_postgres=is_pg,
            term=query,
            fields=["a.title", "a.author", "a.digest"],
        )
        where.append(clause)
        count_params.extend(values)
    if since_ts is not None:
        where.append("a.publish_at >= %s" if is_pg else "a.publish_at >= ?")
        count_params.append(since_ts)
    if until_ts is not None:
        where.append("a.publish_at <= %s" if is_pg else "a.publish_at <= ?")
        count_params.append(until_ts)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    count_sql = (
        "SELECT COUNT(*) AS total"
        " FROM articles a"
        " JOIN accounts acc ON acc.biz = a.biz"
        f" {where_sql}"
    )
    total_row = _fetchone(storage, count_sql, count_params)
    total = int(total_row["total"]) if total_row else 0
    return {"articles": rows, "page": page, "page_size": page_size, "total": total}


def _get_article(storage: StorageLike, article_id: int) -> dict[str, Any]:
    article = _fetchone(
        storage,
        (
            "SELECT a.id, a.biz, a.article_id, a.title, a.author, a.digest, a.cover, a.link,"
            " a.source_url, a.publish_at,"
            " acc.nickname AS account_nickname, acc.alias AS account_alias,"
            " acc.round_head_img AS account_avatar, acc.group_id, g.name AS group_name"
            " FROM articles a"
            " JOIN accounts acc ON acc.biz = a.biz"
            " LEFT JOIN account_groups g ON g.id = acc.group_id"
            " WHERE a.id = %s"
        )
        if _is_postgres(storage)
        else (
            "SELECT a.id, a.biz, a.article_id, a.title, a.author, a.digest, a.cover, a.link,"
            " a.source_url, a.publish_at,"
            " acc.nickname AS account_nickname, acc.alias AS account_alias,"
            " acc.round_head_img AS account_avatar, acc.group_id, g.name AS group_name"
            " FROM articles a"
            " JOIN accounts acc ON acc.biz = a.biz"
            " LEFT JOIN account_groups g ON g.id = acc.group_id"
            " WHERE a.id = ?"
        ),
        [article_id],
    )
    if not article:
        raise ApiError("Article not found", status=404)

    content_row = _fetchone(
        storage,
        "SELECT content_json, clean_html FROM article_content WHERE article_pk = %s"
        if _is_postgres(storage)
        else "SELECT content_json, clean_html FROM article_content WHERE article_pk = ?",
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
        "SELECT id, position, kind, content_type FROM article_images WHERE article_pk = %s ORDER BY position ASC"
        if _is_postgres(storage)
        else "SELECT id, position, kind, content_type FROM article_images WHERE article_pk = ? ORDER BY position ASC",
        [article_id],
    )
    return {
        "article": article,
        "content": content_json,
        "clean_html": clean_html,
        "images": images,
    }


def _list_article_images(storage: StorageLike, article_id: int) -> list[dict[str, Any]]:
    return _fetchall(
        storage,
        "SELECT id, position, kind, content_type FROM article_images WHERE article_pk = %s ORDER BY position ASC"
        if _is_postgres(storage)
        else "SELECT id, position, kind, content_type FROM article_images WHERE article_pk = ? ORDER BY position ASC",
        [article_id],
    )


def _fetch_image(storage: StorageLike, image_id: int) -> tuple[bytes, str]:
    row = _fetchone(
        storage,
        "SELECT data, content_type FROM article_images WHERE id = %s"
        if _is_postgres(storage)
        else "SELECT data, content_type FROM article_images WHERE id = ?",
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
        limit=limit,
        offset=0,
    )
    return _fetchall(storage, query_sql, params)


class HippoHandler(BaseHTTPRequestHandler):
    server_version = "HippoHTTP/1.0"

    def do_OPTIONS(self) -> None:  # pragma: no cover - simple CORS support
        self.send_response(HTTPStatus.NO_CONTENT)
        self._set_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._handle_api("GET", parsed)
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._handle_api("POST", parsed)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_PATCH(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._handle_api("PATCH", parsed)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_DELETE(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._handle_api("DELETE", parsed)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, fmt: str, *args: Any) -> None:  # pragma: no cover
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _set_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._set_cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ApiError("Invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise ApiError("JSON body must be an object")
        return payload

    def _serve_static(self, raw_path: str) -> None:
        static_dir = self.server.static_dir  # type: ignore[attr-defined]
        path = raw_path
        if path == "/":
            path = "/index.html"
        target = (static_dir / path.lstrip("/")).resolve()
        if not str(target).startswith(str(static_dir.resolve())):
            self._send_error(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        if not target.exists() or not target.is_file():
            self._send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        content_type, _ = mimetypes.guess_type(str(target))
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_api(self, method: str, parsed: Any) -> None:
        segments = [seg for seg in parsed.path.split("/") if seg]
        query = parse_qs(parsed.query)
        try:
            with open_storage(DB_PATH) as storage:
                _ensure_default_group(storage)
                if len(segments) < 2:
                    raise ApiError("Invalid API path", status=404)
                resource = segments[1]
                if resource == "group":
                    self._handle_group(storage, method, segments, query)
                elif resource == "account":
                    self._handle_account(storage, method, segments, query)
                elif resource == "article":
                    self._handle_article(storage, method, segments, query)
                elif resource == "image":
                    self._handle_image(storage, method, segments)
                elif resource == "feed":
                    self._handle_feed(storage, method, segments, query)
                elif resource == "login":
                    self._handle_login(storage, method, segments)
                elif resource == "sync":
                    self._handle_sync(storage, method, segments, query)
                else:
                    raise ApiError("Not found", status=404)
        except ApiError as exc:
            self._send_error(HTTPStatus(exc.status), str(exc))
        except Exception as exc:  # pragma: no cover - guardrail
            logger.exception("API error")
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def _handle_group(
        self,
        storage: StorageLike,
        method: str,
        segments: list[str],
        query: dict[str, list[str]],
    ) -> None:
        if len(segments) == 2:
            if method == "GET":
                default_group = _ensure_default_group(storage)
                payload = {
                    "default_group_id": default_group["id"],
                    "groups": _list_groups(storage),
                }
                self._send_json(HTTPStatus.OK, payload)
                return
            if method == "POST":
                body = self._read_json()
                name = str(body.get("name", "")).strip()
                if not name:
                    raise ApiError("Group name is required")
                group = storage.upsert_group(name)
                payload = {"id": group.id, "name": group.name}
                self._send_json(HTTPStatus.CREATED, payload)
                return
            raise ApiError("Method not allowed", status=405)

        if len(segments) == 3:
            group_id = _parse_int(segments[2])
            if group_id is None:
                raise ApiError("Group id is required")
            if method == "GET":
                self._send_json(HTTPStatus.OK, _get_group(storage, group_id))
                return
            if method == "PATCH":
                body = self._read_json()
                name = str(body.get("name", "")).strip()
                if not name:
                    raise ApiError("Group name is required")
                updated = _update_group(storage, group_id, name)
                self._send_json(HTTPStatus.OK, updated)
                return
            if method == "DELETE":
                _delete_group(storage, group_id)
                self._send_json(HTTPStatus.NO_CONTENT, {})
                return
        raise ApiError("Not found", status=404)

    def _handle_account(
        self,
        storage: StorageLike,
        method: str,
        segments: list[str],
        query: dict[str, list[str]],
    ) -> None:
        if len(segments) == 2:
            if method == "GET":
                group_id = _parse_int(query.get("group_id", [None])[0])
                search = query.get("q", [""])[0] or None
                page = _parse_int(query.get("page", ["1"])[0]) or 1
                page_size = _parse_int(query.get("page_size", ["20"])[0]) or 20
                payload = _list_accounts(
                    storage,
                    group_id=group_id,
                    query=search,
                    page=max(page, 1),
                    page_size=min(max(page_size, 1), 200),
                )
                self._send_json(HTTPStatus.OK, payload)
                return
            if method == "POST":
                body = self._read_json()
                required = ["biz", "nickname"]
                for field in required:
                    if not body.get(field):
                        raise ApiError(f"{field} is required")
                group_id = body.get("group_id")
                if group_id is None:
                    default_group = _ensure_default_group(storage)
                    group_id = default_group["id"]
                account = storage.upsert_account(
                    AccountCredential(
                        biz=str(body["biz"]),
                        nickname=str(body["nickname"]),
                        alias=body.get("alias"),
                        round_head_img=body.get("round_head_img"),
                        uin=str(body.get("uin") or ""),
                        key=str(body.get("key") or ""),
                        pass_ticket=str(body.get("pass_ticket") or ""),
                        group_id=int(group_id) if group_id is not None else None,
                    )
                )
                self._send_json(
                    HTTPStatus.CREATED,
                    {
                        "biz": account.biz,
                        "nickname": account.nickname,
                        "alias": account.alias,
                        "round_head_img": account.round_head_img,
                        "group_id": account.group_id,
                    },
                )
                return
            raise ApiError("Method not allowed", status=405)

        if len(segments) == 3:
            if segments[2] == "move":
                if method != "POST":
                    raise ApiError("Method not allowed", status=405)
                body = self._read_json()
                biz_list = body.get("biz_list") or []
                if not isinstance(biz_list, list) or not biz_list:
                    raise ApiError("biz_list is required")
                group_id = body.get("group_id")
                if group_id is None:
                    default_group = _ensure_default_group(storage)
                    group_id = default_group["id"]
                is_pg = _is_postgres(storage)
                if is_pg:
                    with storage.conn.cursor() as cur:
                        cur.execute(
                            "UPDATE accounts SET group_id = %s, updated_at = NOW() WHERE biz = ANY(%s)",
                            (group_id, biz_list),
                        )
                    storage.conn.commit()
                else:
                    placeholders = ",".join(["?"] * len(biz_list))
                    storage.conn.execute(
                        f"UPDATE accounts SET group_id = ?, updated_at = ? WHERE biz IN ({placeholders})",
                        [group_id, datetime.utcnow().isoformat(), *biz_list],
                    )
                    storage.conn.commit()
                self._send_json(HTTPStatus.OK, {"updated": len(biz_list)})
                return
            biz = segments[2]
            if method == "GET":
                self._send_json(HTTPStatus.OK, _get_account(storage, biz))
                return
            if method == "PATCH":
                body = self._read_json()
                if "group_id" in body and body["group_id"] is None:
                    default_group = _ensure_default_group(storage)
                    body["group_id"] = default_group["id"]
                updated = _update_account(storage, biz, body)
                self._send_json(HTTPStatus.OK, updated)
                return
            if method == "DELETE":
                removed = storage.remove_account(biz)
                if removed == 0:
                    raise ApiError("Account not found", status=404)
                self._send_json(HTTPStatus.NO_CONTENT, {})
                return
        raise ApiError("Not found", status=404)

    def _handle_article(
        self,
        storage: StorageLike,
        method: str,
        segments: list[str],
        query: dict[str, list[str]],
    ) -> None:
        if len(segments) == 2:
            if method == "GET":
                group_id = _parse_int(query.get("group_id", [None])[0])
                biz = query.get("biz", [""])[0] or None
                search = query.get("q", [""])[0] or None
                page = _parse_int(query.get("page", ["1"])[0]) or 1
                page_size = _parse_int(query.get("page_size", ["20"])[0]) or 20
                since_ts = _parse_date(query.get("since", [None])[0])
                until_ts = _parse_date(query.get("until", [None])[0], end_of_day=True)
                payload = _list_articles(
                    storage,
                    group_id=group_id,
                    biz=biz,
                    query=search,
                    since_ts=since_ts,
                    until_ts=until_ts,
                    page=max(page, 1),
                    page_size=min(max(page_size, 1), 200),
                )
                self._send_json(HTTPStatus.OK, payload)
                return
            raise ApiError("Method not allowed", status=405)

        if len(segments) == 3:
            article_id = _parse_int(segments[2])
            if article_id is None:
                raise ApiError("Article id is required")
            if method == "GET":
                payload = _get_article(storage, article_id)
                self._send_json(HTTPStatus.OK, payload)
                return
        if len(segments) == 4 and segments[3] == "image":
            article_id = _parse_int(segments[2])
            if article_id is None:
                raise ApiError("Article id is required")
            if method == "GET":
                payload = _list_article_images(storage, article_id)
                self._send_json(HTTPStatus.OK, {"images": payload})
                return
        raise ApiError("Not found", status=404)

    def _handle_image(self, storage: StorageLike, method: str, segments: list[str]) -> None:
        if len(segments) != 3:
            raise ApiError("Not found", status=404)
        if method != "GET":
            raise ApiError("Method not allowed", status=405)
        image_id = _parse_int(segments[2])
        if image_id is None:
            raise ApiError("Image id is required")
        payload, content_type = _fetch_image(storage, image_id)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_login(
        self,
        storage: StorageLike,
        method: str,
        segments: list[str],
    ) -> None:
        manager: LoginManager = self.server.login_manager  # type: ignore[attr-defined]

        def payload_from(snapshot: dict[str, Any]) -> dict[str, Any]:
            info = _get_login_info(storage)
            return {
                **snapshot,
                "qrcode_url": "/api/login/qrcode" if snapshot.get("has_qrcode") else None,
                "last_login": info,
            }

        if len(segments) == 2:
            if method == "GET":
                self._send_json(HTTPStatus.OK, payload_from(manager._snapshot()))
                return
            raise ApiError("Method not allowed", status=405)

        if len(segments) == 3:
            action = segments[2]
            if action == "start" and method == "POST":
                snapshot = manager.start()
                self._send_json(HTTPStatus.OK, payload_from(snapshot))
                return
            if action == "poll" and method == "POST":
                snapshot = manager.poll(storage)
                self._send_json(HTTPStatus.OK, payload_from(snapshot))
                return
            if action == "cancel" and method == "POST":
                manager.cancel()
                self._send_json(HTTPStatus.OK, payload_from(manager._snapshot()))
                return
            if action == "qrcode" and method == "GET":
                data = manager.get_qrcode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        raise ApiError("Not found", status=404)

    def _handle_sync(
        self,
        storage: StorageLike,
        method: str,
        segments: list[str],
        query: dict[str, list[str]],
    ) -> None:
        scheduler: SyncScheduler = self.server.sync_scheduler  # type: ignore[attr-defined]
        if len(segments) == 2:
            if method == "GET":
                payload = _get_sync_status(storage)
                self._send_json(HTTPStatus.OK, payload)
                return
            raise ApiError("Method not allowed", status=405)

        if len(segments) == 3:
            action = segments[2]
            if action == "settings":
                if method == "GET":
                    self._send_json(HTTPStatus.OK, _get_sync_settings(storage))
                    return
                if method == "PATCH":
                    body = self._read_json()
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
                        updates["page_limit"] = body["page_limit"]
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
                    settings = _set_sync_settings(storage, updates)
                    if settings.get("enabled"):
                        scheduler.trigger()
                    self._send_json(HTTPStatus.OK, settings)
                    return
            if action == "run" and method == "POST":
                threading.Thread(target=scheduler.run_once, daemon=True).start()
                self._send_json(HTTPStatus.ACCEPTED, {"status": "running"})
                return
        raise ApiError("Not found", status=404)

    def _handle_feed(
        self,
        storage: StorageLike,
        method: str,
        segments: list[str],
        query: dict[str, list[str]],
    ) -> None:
        if len(segments) != 3 or segments[2] != "mixed":
            raise ApiError("Not found", status=404)
        if method != "GET":
            raise ApiError("Method not allowed", status=405)
        group_id = _parse_int(query.get("group_id", [None])[0])
        biz = query.get("biz", [""])[0] or None
        search = query.get("q", [""])[0] or None
        limit = _parse_int(query.get("limit", ["50"])[0]) or 50
        since_ts = _parse_date(query.get("since", [None])[0])
        until_ts = _parse_date(query.get("until", [None])[0], end_of_day=True)
        days = _parse_int(query.get("days", [None])[0])
        if days:
            now = datetime.utcnow()
            since_ts = int((now.timestamp() - days * 86400))
        payload = _list_feed(
            storage,
            group_id=group_id,
            biz=biz,
            query=search,
            since_ts=since_ts,
            until_ts=until_ts,
            limit=min(max(limit, 1), 500),
        )
        self._send_json(HTTPStatus.OK, {"articles": payload})


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, static_dir: Path | str = "static") -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    static_path = Path(static_dir).expanduser().resolve()
    if not static_path.exists():
        raise RuntimeError(f"Static directory not found: {static_path}")
    httpd = ThreadingHTTPServer((host, port), HippoHandler)
    httpd.static_dir = static_path  # type: ignore[attr-defined]
    httpd.login_manager = LoginManager()  # type: ignore[attr-defined]
    httpd.sync_scheduler = SyncScheduler()  # type: ignore[attr-defined]
    httpd.sync_scheduler.start()  # type: ignore[attr-defined]
    logger.info("Hippo server listening on http://%s:%s", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        httpd.sync_scheduler.stop()  # type: ignore[attr-defined]
        httpd.server_close()
