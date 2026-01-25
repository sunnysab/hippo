"""Minimal HTTP server for Hippo API + static UI."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import threading
import time as time_module
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

import httpx
import psycopg
from fastapi import APIRouter, Body, Depends, FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

try:
    import jieba
except Exception:  # pragma: no cover - optional fallback
    jieba = None

from .emailer import get_email_settings, set_email_settings
from .http import MPClient, SessionExpiredError
from .models import AccountCredential
from .rss import build_rss_xml, query_rss_items
from .s3 import build_image_key, fetch_object_bytes, get_s3_client, upload_object_bytes
from .storage import PostgresStorage, open_storage
from .sync_service import (
    SyncScheduler,
    get_sync_settings as load_sync_settings,
    get_sync_status as load_sync_status,
    set_sync_settings as save_sync_settings,
)
from .utils import ensure_default_group, fetchall_rows, fetchone_row, parse_iso_date_to_timestamp

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_GROUP_NAME = "Default"

logger = logging.getLogger("hippo.serve")


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status




def _parse_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(f"Invalid integer: {value}") from exc


_SYNC_MODES = {'incremental', 'recent', 'full', 'range'}


def _normalize_sync_mode(value: Any) -> str | None:
    if value in (None, ""):
        return None
    mode = str(value).strip().lower()
    if not mode:
        return None
    if mode not in _SYNC_MODES:
        raise ApiError('Invalid sync mode', status=400)
    return mode


def _normalize_recent_days(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError('Invalid recent days') from exc
    if days < 1:
        raise ApiError('Invalid recent days', status=400)
    return days


def _parse_date(value: str | None, *, end_of_day: bool = False) -> int | None:
    try:
        return parse_iso_date_to_timestamp(value, end_of_day=end_of_day)
    except ValueError as exc:
        raise ApiError(f"Invalid date: {value}") from exc


def _normalize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: _normalize_value(value) for key, value in record.items()}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_login_info(storage: PostgresStorage) -> dict[str, Any] | None:
    row = fetchone_row(
        storage,
        "SELECT nickname, avatar, updated_at FROM login_sessions ORDER BY id DESC LIMIT 1",
        [],
        normalize=_normalize_record,
    )
    return row


class LoginManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._uuid_cookie: str | None = None
        self._qrcode: bytes | None = None
        self._status: str = "idle"
        self._message: str = ""
        self._updated_at: str | None = None

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

    async def poll(self, storage: PostgresStorage) -> dict[str, Any]:
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






def _ensure_avatar_images_table(storage: PostgresStorage) -> None:
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


def _migrate_legacy_avatar_tables(storage: PostgresStorage) -> None:
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




def _list_groups(storage: PostgresStorage) -> list[dict[str, Any]]:
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


def _get_group(storage: PostgresStorage, group_id: int) -> dict[str, Any]:
    row = fetchone_row(
        storage,
        """
        SELECT g.id, g.name, g.sync_mode, g.sync_recent_days, COUNT(a.biz) AS account_count
        FROM account_groups g
        LEFT JOIN accounts a ON a.group_id = g.id
        WHERE g.id = %s
        GROUP BY g.id, g.name, g.sync_mode, g.sync_recent_days
        """,
        [group_id],
        normalize=_normalize_record,
    )
    if not row:
        raise ApiError("Group not found", status=404)
    return row


def _update_group(storage: PostgresStorage, group_id: int, updates: dict[str, Any]) -> dict[str, Any]:
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


def _delete_group(storage: PostgresStorage, group_id: int) -> None:
    default_group = ensure_default_group(storage, name=DEFAULT_GROUP_NAME)
    default_id = default_group.id
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
    storage: PostgresStorage,
    *,
    group_id: int | None,
    query: str | None,
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
    rows = fetchall_rows(storage, query_sql, params + [page_size, offset], normalize=_normalize_record)
    for row in rows:
        row["avatar_url"] = f"/api/account/{row['biz']}/avatar"
    count_sql = (
        "SELECT COUNT(*) AS total FROM accounts a"
        " LEFT JOIN account_groups g ON g.id = a.group_id"
        f" {where_sql}"
    )
    total_row = fetchone_row(storage, count_sql, params, normalize=_normalize_record)
    total = int(total_row["total"]) if total_row else 0
    return {"accounts": rows, "page": page, "page_size": page_size, "total": total}


def _get_account(storage: PostgresStorage, biz: str) -> dict[str, Any]:
    row = fetchone_row(
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
        normalize=_normalize_record,
    )
    if not row:
        raise ApiError("Account not found", status=404)
    row["avatar_url"] = f"/api/account/{row['biz']}/avatar"
    return row


def _update_account(storage: PostgresStorage, biz: str, payload: dict[str, Any]) -> dict[str, Any]:
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
    storage: PostgresStorage,
    group_id: int | None,
    biz: str | None,
    query: str | None,
    since_ts: int | None,
    until_ts: int | None,
    content_only: bool,
    limit: int,
    offset: int,
    article_id: str | None = None,
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
        "  WHERE i.article_pk = a.id AND i.s3_key IS NOT NULL AND i.s3_key <> ''"
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
    storage: PostgresStorage,
    *,
    group_id: int | None,
    biz: str | None,
    query: str | None,
    since_ts: int | None,
    until_ts: int | None,
    content_only: bool,
    page: int,
    page_size: int,
    article_id: str | None = None,
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
    rows = fetchall_rows(storage, query_sql, params, normalize=_normalize_record)
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
    total_row = fetchone_row(storage, count_sql, count_params, normalize=_normalize_record)
    total = int(total_row["total"]) if total_row else 0
    return {"articles": rows, "page": page, "page_size": page_size, "total": total}


def _get_article(storage: PostgresStorage, article_id: int) -> dict[str, Any]:
    article = fetchone_row(
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
        normalize=_normalize_record,
    )
    if not article:
        raise ApiError("Article not found", status=404)
    article["account_avatar_url"] = f"/api/account/{article['biz']}/avatar"

    content_row = fetchone_row(
        storage,
        "SELECT content_json, clean_html FROM article_content WHERE article_pk = %s",
        [article_id],
        normalize=_normalize_record,
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

    images = fetchall_rows(
        storage,
        "SELECT id, position, kind, content_type FROM article_images WHERE article_pk = %s ORDER BY position ASC",
        [article_id],
        normalize=_normalize_record,
    )
    return {
        "article": article,
        "content": content_json,
        "images": images,
    }


def _list_article_images(storage: PostgresStorage, article_id: int) -> list[dict[str, Any]]:
    return fetchall_rows(
        storage,
        "SELECT id, position, kind, content_type FROM article_images WHERE article_pk = %s ORDER BY position ASC",
        [article_id],
        normalize=_normalize_record,
    )


def _fetch_image(storage: PostgresStorage, image_id: int) -> tuple[bytes, str]:
    row = fetchone_row(
        storage,
        (
            "SELECT i.content_type, i.s3_key, i.orig_url, a.link AS referer"
            " FROM article_images i"
            " JOIN articles a ON a.id = i.article_pk"
            " WHERE i.id = %s"
        ),
        [image_id],
        normalize=_normalize_record,
    )
    if not row:
        raise ApiError("Image not found", status=404)
    content_type = row.get('content_type')
    s3_key = row.get('s3_key')
    if s3_key:
        bundle = get_s3_client()
        if bundle:
            config, client = bundle
            try:
                payload, s3_content_type = fetch_object_bytes(
                    client,
                    bucket=config.bucket,
                    key=str(s3_key),
                )
                resolved_type = s3_content_type or content_type or 'application/octet-stream'
                return payload, resolved_type
            except Exception as exc:
                logger.warning('S3 image fetch failed (id=%s key=%s): %s', image_id, s3_key, exc)
    orig_url = row.get('orig_url')
    if not orig_url:
        raise ApiError('Image data missing', status=404)
    referer = row.get('referer')
    try:
        payload, fetched_type = _download_image_from_origin(str(orig_url), referer=referer)
    except Exception as exc:
        logger.warning('Origin image fetch failed (id=%s url=%s): %s', image_id, orig_url, exc)
        raise ApiError('Image fetch failed', status=502) from exc
    resolved_type = fetched_type or content_type or 'application/octet-stream'
    _store_image_to_s3_async(
        image_id=image_id,
        payload=payload,
        content_type=resolved_type,
        s3_key=str(s3_key) if s3_key else None,
    )
    return payload, resolved_type


def _download_image_from_origin(
    orig_url: str, *, referer: str | None
) -> tuple[bytes, str | None]:
    async def _run() -> tuple[bytes, str | None]:
        async with MPClient() as client:
            return await client.download_binary_with_type(orig_url, referer=referer)

    return asyncio.run(_run())


def _store_image_to_s3_async(
    *, image_id: int, payload: bytes, content_type: str | None, s3_key: str | None
) -> None:
    def _worker() -> None:
        bundle = get_s3_client()
        if not bundle:
            return
        config, client = bundle
        resolved_key = s3_key or build_image_key(config.prefix, image_id, content_type)
        try:
            upload_object_bytes(
                client,
                bucket=config.bucket,
                key=resolved_key,
                payload=payload,
                content_type=content_type,
            )
            with open_storage() as storage:
                with storage.conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE article_images
                        SET s3_key = %s,
                            content_type = %s,
                            updated_at = %s
                        WHERE id = %s
                        """,
                        (resolved_key, content_type, _utc_now_iso(), image_id),
                    )
                storage.conn.commit()
        except Exception as exc:
            logger.warning('S3 image store failed (id=%s key=%s): %s', image_id, resolved_key, exc)

    threading.Thread(target=_worker, daemon=True).start()


def _get_avatar_row(storage: PostgresStorage, biz: str) -> dict[str, Any] | None:
    return fetchone_row(
        storage,
        "SELECT avatar_url, content_type, data FROM avatar_images WHERE biz = %s",
        [biz],
        normalize=_normalize_record,
    )


def _upsert_avatar_url(storage: PostgresStorage, biz: str, url: str) -> None:
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
    storage: PostgresStorage,
    biz: str,
    *,
    content_type: str,
    data: bytes,
    avatar_url: str | None = None,
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
            (biz, avatar_url, content_type, psycopg.Binary(data), _utc_now_iso()),
        )
    storage.conn.commit()


def _fetch_and_cache_avatar(storage: PostgresStorage, biz: str, url: str) -> tuple[bytes, str] | None:
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
    storage: PostgresStorage,
    *,
    group_id: int | None,
    biz: str | None,
    query: str | None,
    since_ts: int | None,
    until_ts: int | None,
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
    return fetchall_rows(storage, query_sql, params, normalize=_normalize_record)




def _binary_response(payload: bytes, content_type: str) -> Response:
    return Response(
        content=payload,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=259200"},
    )


def _get_storage() -> Generator[PostgresStorage, None, None]:
    with open_storage() as storage:
        ensure_default_group(storage, name=DEFAULT_GROUP_NAME)
        _ensure_avatar_images_table(storage)
        yield storage


def _get_login_manager(request: Request) -> LoginManager:
    return request.app.state.login_manager


def _get_sync_scheduler(request: Request) -> SyncScheduler:
    return request.app.state.sync_scheduler


router = APIRouter(prefix="/api")


@router.get("/group")
def list_groups(storage: PostgresStorage = Depends(_get_storage)) -> dict[str, Any]:
    default_group = ensure_default_group(storage, name=DEFAULT_GROUP_NAME)
    return {
        "default_group_id": default_group.id,
        "groups": _list_groups(storage),
    }


@router.post("/group", status_code=status.HTTP_201_CREATED)
def create_group(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    name = str(body.get("name", "")).strip()
    if not name:
        raise ApiError("Group name is required")
    group = storage.upsert_group(name)
    return {"id": group.id, "name": group.name}


@router.get("/group/{group_id}")
def get_group(
    group_id: int,
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    return _get_group(storage, group_id)


@router.patch("/group/{group_id}")
def update_group(
    group_id: int,
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(_get_storage),
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
    storage: PostgresStorage = Depends(_get_storage),
) -> Response:
    _delete_group(storage, group_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/account/search")
async def search_account(
    q: str = "",
    page: int = 1,
    page_size: int = 10,
    begin: int | None = None,
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    keyword = (q or "").strip()
    if not keyword:
        raise ApiError("q is required")
    offset = begin if begin is not None else (max(page, 1) - 1) * page_size
    _ensure_avatar_images_table(storage)
    existing = {account.biz for account in storage.list_accounts()}
    session = storage.get_login_session()
    async with MPClient() as client:
        try:
            payload = await client.search_biz(
                session,
                keyword=keyword,
                begin=offset,
                count=min(max(page_size, 1), 20),
            )
        except SessionExpiredError as exc:
            raise ApiError("Session expired. Please login again.", status=401) from exc
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
    storage: PostgresStorage = Depends(_get_storage),
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
    group_id: int | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
    storage: PostgresStorage = Depends(_get_storage),
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
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    required = ["biz", "nickname"]
    for field in required:
        if not body.get(field):
            raise ApiError(f"{field} is required")
    group_id = body.get("group_id")
    if group_id is None:
        default_group = ensure_default_group(storage, name=DEFAULT_GROUP_NAME)
        group_id = default_group.id
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
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    biz_list = body.get("biz_list") or []
    if not isinstance(biz_list, list) or not biz_list:
        raise ApiError("biz_list is required")
    group_id = body.get("group_id")
    if group_id is None:
        default_group = ensure_default_group(storage, name=DEFAULT_GROUP_NAME)
        group_id = default_group.id
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
    storage: PostgresStorage = Depends(_get_storage),
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
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    return _get_account(storage, biz)


@router.patch("/account/{biz}")
def update_account(
    biz: str,
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    if "group_id" in body and body["group_id"] is None:
        default_group = ensure_default_group(storage, name=DEFAULT_GROUP_NAME)
        body["group_id"] = default_group.id
    return _update_account(storage, biz, body)


@router.delete("/account/{biz}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    biz: str,
    storage: PostgresStorage = Depends(_get_storage),
) -> Response:
    removed = storage.remove_account(biz)
    if removed == 0:
        raise ApiError("Account not found", status=404)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/account/{biz}/avatar")
def get_account_avatar(
    biz: str,
    storage: PostgresStorage = Depends(_get_storage),
) -> Response:
    avatar = _get_avatar_row(storage, biz)
    data = avatar.get("data") if avatar else None
    if not data:
        url = avatar.get("avatar_url") if avatar else None
        if not url:
            row = fetchone_row(
                storage,
                "SELECT round_head_img FROM accounts WHERE biz = %s",
                [biz],
                normalize=_normalize_record,
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
    group_id: int | None = None,
    biz: str | None = None,
    article_id: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
    content: str = "",
    since: str | None = None,
    until: str | None = None,
    storage: PostgresStorage = Depends(_get_storage),
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
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    return _get_article(storage, article_id)


@router.get("/article/{article_id}/image")
def list_article_images(
    article_id: int,
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    payload = _list_article_images(storage, article_id)
    return {"images": payload}


@router.get("/image/{image_id}")
def get_image(
    image_id: int,
    storage: PostgresStorage = Depends(_get_storage),
) -> Response:
    payload, content_type = _fetch_image(storage, image_id)
    return _binary_response(payload, content_type)


@router.get("/login")
def login_status(
    storage: PostgresStorage = Depends(_get_storage),
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
    storage: PostgresStorage = Depends(_get_storage),
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
    storage: PostgresStorage = Depends(_get_storage),
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
    storage: PostgresStorage = Depends(_get_storage),
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
def sync_status(storage: PostgresStorage = Depends(_get_storage)) -> dict[str, Any]:
    return load_sync_status(storage)


@router.get("/sync/settings")
def get_sync_settings(storage: PostgresStorage = Depends(_get_storage)) -> dict[str, Any]:
    payload = load_sync_settings(storage)
    payload["email"] = get_email_settings(storage)
    return payload


@router.patch("/sync/settings")
def update_sync_settings(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(_get_storage),
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
    settings = save_sync_settings(storage, updates)
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
        settings["email"] = set_email_settings(storage, email_updates)
    else:
        settings["email"] = get_email_settings(storage)
    if settings.get("enabled"):
        scheduler.trigger()
    return settings


@router.post("/sync/run", status_code=status.HTTP_202_ACCEPTED)
def run_sync(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(_get_storage),
    scheduler: "SyncScheduler" = Depends(_get_sync_scheduler),
) -> dict[str, Any]:
    group_id = body.get('group_id')
    if group_id is not None:
        try:
            group_id = int(group_id)
        except (TypeError, ValueError) as exc:
            raise ApiError('Invalid group_id') from exc
        row = fetchone_row(
            storage,
            'SELECT id FROM account_groups WHERE id = %s',
            [group_id],
            normalize=_normalize_record,
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
    group_id: int | None = None,
    biz: str | None = None,
    q: str | None = None,
    limit: int = 50,
    format: str | None = None,
    since: str | None = None,
    until: str | None = None,
    days: int | None = None,
    storage: PostgresStorage = Depends(_get_storage),
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
            row = fetchone_row(
                storage,
                "SELECT name FROM account_groups WHERE id = %s",
                [group_id],
                normalize=_normalize_record,
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
