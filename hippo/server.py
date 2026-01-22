"""Minimal HTTP server for Hippo API + static UI."""

from __future__ import annotations

import json
import logging
import mimetypes
from datetime import date, datetime, time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import psycopg2
import psycopg2.extras

from .config import DB_PATH
from .models import AccountCredential
from .storage import PostgresStorage, StorageLike, open_storage

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_GROUP_NAME = "Default"

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
    logger.info("Hippo server listening on http://%s:%s", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        httpd.server_close()
