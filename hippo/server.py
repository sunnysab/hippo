"""Minimal HTTP server for Hippo API + frontend UI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import socket
import stat
import threading
import time as time_module
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

try:
    import jieba
except Exception:  # pragma: no cover - optional fallback
    jieba = None

from .article_queries import (
    _block_image,
    _fetch_image,
    _get_article,
    _list_article_images,
    _list_articles,
    _list_feed,
    _normalize_article_sort,
    _normalize_item_show_type,
    _normalize_recent_days,
    _normalize_record,
    _normalize_sync_mode,
    _parse_date,
    _split_article_exclude_keywords,
    _tokenize_query,
)
from .avatar import (
    _ensure_avatar_images_table,
    _fetch_and_cache_avatar,
    _get_avatar_row,
    _upsert_avatar_url,
)
from .config import DEFAULT_GROUP_NAME
from .container import build_downloader_container
from .emailer import get_email_settings, send_email, set_email_settings
from .exceptions import ApiError
from .http import MPClient
from .login_service import save_login_session
from .models import AccountCredential
from .rss import build_rss_xml, query_rss_items
from .storage import PostgresStorage, ensure_default_group, fetchone_row, open_storage
from .sync_core import request_sync_cancel
from .sync_service import (
    SyncScheduler,
)
from .sync_service import (
    get_sync_settings as load_sync_settings,
)
from .sync_service import (
    get_sync_status as load_sync_status,
)
from .sync_service import (
    set_sync_settings as save_sync_settings,
)
from .utils import utc_now_iso
from .wechat_api import SessionExpiredError, WeChatApiClient

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8000
DEFAULT_LOG_LEVEL = 'WARNING'
_LOG_LEVEL_MAP = {
    'CRITICAL': logging.CRITICAL,
    'ERROR': logging.ERROR,
    'WARNING': logging.WARNING,
    'INFO': logging.INFO,
    'DEBUG': logging.DEBUG,
}
_INPROCESS_SYNC_VALUES = {'1', 'true', 'yes', 'on'}

logger = logging.getLogger('hippo.serve')


def _resolve_log_level() -> tuple[int, str]:
    level_name = str(os.environ.get('HIPPO_LOG_LEVEL') or DEFAULT_LOG_LEVEL).strip().upper()
    if level_name not in _LOG_LEVEL_MAP:
        level_name = DEFAULT_LOG_LEVEL
    return _LOG_LEVEL_MAP[level_name], level_name.lower()


def _inprocess_sync_enabled() -> bool:
    return os.environ.get('HIPPO_ENABLE_INPROCESS_SYNC', '').strip().lower() in _INPROCESS_SYNC_VALUES


def _normalize_listen_host(host: str | None) -> str | None:
    if host is None:
        return None
    normalized = host.strip()
    return normalized or None


def _normalize_unix_socket_path(unix_socket: Path | str | None) -> Path | None:
    if unix_socket is None:
        return None
    normalized = str(unix_socket).strip()
    if not normalized:
        return None
    return Path(normalized)


def _remove_stale_unix_socket(path: Path) -> None:
    try:
        existing = path.stat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(existing.st_mode):
        raise RuntimeError(f'Unix socket path already exists and is not a socket: {path}')
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.1)
        probe.connect(str(path))
    except ConnectionRefusedError, FileNotFoundError:
        pass
    except OSError as exc:
        raise RuntimeError(f'Failed to inspect Unix socket path {path}: {exc}') from exc
    else:
        raise RuntimeError(f'Unix socket path is already in use: {path}')
    finally:
        probe.close()
    path.unlink()


def _create_tcp_listen_socket(host: str, port: int) -> socket.socket:
    last_error: OSError | None = None
    addrinfo = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    for family, socktype, proto, _, sockaddr in addrinfo:
        candidate = socket.socket(family, socktype, proto)
        try:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            candidate.bind(sockaddr)
            candidate.set_inheritable(True)
            return candidate
        except OSError as exc:
            last_error = exc
            candidate.close()
    raise RuntimeError(f'Failed to bind TCP listener on {host}:{port}') from last_error


def _create_unix_listen_socket(path: Path, mode: int) -> socket.socket:
    if not hasattr(socket, 'AF_UNIX'):
        raise RuntimeError('Unix sockets are not supported on this platform')
    if not path.parent.exists():
        raise RuntimeError(f'Unix socket parent directory does not exist: {path.parent}')
    if not path.parent.is_dir():
        raise RuntimeError(f'Unix socket parent path is not a directory: {path.parent}')
    _remove_stale_unix_socket(path)
    candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound = False
    try:
        candidate.bind(str(path))
        bound = True
        os.chmod(path, mode)
        candidate.set_inheritable(True)
        return candidate
    except OSError as exc:
        candidate.close()
        if bound:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        raise RuntimeError(f'Failed to bind Unix socket on {path}: {exc}') from exc


def _build_listen_sockets(
    *,
    host: str | None,
    port: int | None,
    unix_socket: Path | str | None,
    unix_socket_mode: int = 0o660,
) -> list[socket.socket]:
    normalized_host = _normalize_listen_host(host)
    normalized_unix_socket = _normalize_unix_socket_path(unix_socket)
    sockets: list[socket.socket] = []
    try:
        if normalized_host is not None:
            if port is None:
                raise RuntimeError('TCP port is required when host is configured')
            sockets.append(_create_tcp_listen_socket(normalized_host, port))
        elif port is not None:
            raise RuntimeError('TCP host is required when port is configured')

        if normalized_unix_socket is not None:
            sockets.append(_create_unix_listen_socket(normalized_unix_socket, unix_socket_mode))

        if not sockets:
            raise RuntimeError('At least one listener must be configured')
        return sockets
    except Exception:
        for candidate in sockets:
            candidate.close()
        raise


def _parse_int(value: str | None) -> int | None:
    if value is None or value == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(f'Invalid integer: {value}') from exc


def _binary_response(payload: bytes, content_type: str) -> Response:
    return Response(
        content=payload,
        media_type=content_type,
        headers={'Cache-Control': 'public, max-age=259200'},
    )


def _get_login_info(storage: PostgresStorage) -> dict[str, Any] | None:
    row = fetchone_row(
        storage,
        'SELECT nickname, avatar, updated_at FROM login_sessions ORDER BY id DESC LIMIT 1',
        [],
        normalize=_normalize_record,
    )
    return row


def _login_response(
    snapshot: dict[str, Any],
    info: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        **snapshot,
        'qrcode_url': '/api/login/qrcode' if snapshot.get('has_qrcode') else None,
        'last_login': info,
    }


class LoginManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._uuid_cookie: str | None = None
        self._qrcode: bytes | None = None
        self._status: str = 'idle'
        self._message: str = ''
        self._updated_at: str | None = None

    def _snapshot(self) -> dict[str, Any]:
        return {
            'status': self._status,
            'message': self._message,
            'updated_at': self._updated_at,
            'has_qrcode': self._qrcode is not None,
        }

    async def start(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if not force and self._status in ('starting', 'waiting', 'scanned', 'refresh'):
                return self._snapshot()
            if force:
                self._uuid_cookie = None
                self._qrcode = None
            self._status = 'starting'
            self._message = 'Requesting QR code'
            self._updated_at = utc_now_iso()
        sid = f'{int(time_module.time() * 1000)}{random.randint(100, 999)}'
        try:
            async with MPClient(timeout=15.0) as client:
                api_client = WeChatApiClient(client)
                uuid_cookie = await api_client.start_login_session(sid)
                qrcode_bytes = await api_client.fetch_login_qrcode(uuid_cookie)
            with self._lock:
                self._uuid_cookie = uuid_cookie
                self._qrcode = qrcode_bytes
                self._status = 'waiting'
                self._message = 'Scan the QR code with WeChat'
                self._updated_at = utc_now_iso()
            return self._snapshot()
        except Exception as exc:
            with self._lock:
                self._status = 'error'
                self._message = str(exc)
                self._updated_at = utc_now_iso()
            raise ApiError(str(exc)) from exc

    def get_qrcode(self) -> bytes:
        with self._lock:
            if not self._qrcode:
                raise ApiError('QR code not ready', status=404)
            return self._qrcode

    async def poll(self, storage: PostgresStorage) -> dict[str, Any]:
        with self._lock:
            uuid_cookie = self._uuid_cookie
            status = self._status
        if not uuid_cookie:
            if status == 'starting':
                with self._lock:
                    return self._snapshot()
            raise ApiError('Login not started', status=400)
        try:
            async with MPClient(timeout=15.0) as client:
                api_client = WeChatApiClient(client)
                resp = await api_client.check_login_status(uuid_cookie)
                if resp.get('base_resp', {}).get('ret') != 0:
                    raise ApiError('Login status error')
                status = resp.get('status')
                if status == 1:
                    with self._lock:
                        self._status = 'confirmed'
                        self._message = 'Confirmed, completing login...'
                        self._updated_at = utc_now_iso()
                    return self._snapshot()
                if status in (2, 3):
                    qrcode_bytes = await api_client.fetch_login_qrcode(uuid_cookie)
                    with self._lock:
                        self._qrcode = qrcode_bytes
                        self._status = 'refresh'
                        self._message = 'QR code refreshed'
                        self._updated_at = utc_now_iso()
                    return self._snapshot()
                if status in (4, 6):
                    with self._lock:
                        self._status = 'scanned'
                        self._message = 'Scan success, waiting for confirmation'
                        self._updated_at = utc_now_iso()
                    return self._snapshot()
                if status == 5:
                    with self._lock:
                        self._status = 'error'
                        self._message = 'Account cannot login without email'
                        self._updated_at = utc_now_iso()
                    return self._snapshot()
        except ApiError:
            raise
        except Exception as exc:
            with self._lock:
                self._status = 'error'
                self._message = str(exc)
                self._updated_at = utc_now_iso()
            raise ApiError(str(exc)) from exc
        return self._snapshot()

    async def finalize(self, storage: PostgresStorage) -> dict[str, Any]:
        with self._lock:
            uuid_cookie = self._uuid_cookie
        if not uuid_cookie:
            raise ApiError('Login not started', status=400)
        try:
            async with MPClient(timeout=15.0) as client:
                api_client = WeChatApiClient(client)
                session = await api_client.finalize_login(uuid_cookie)
                info = await api_client.fetch_login_info(session)
                session.nickname = info.get('nickname') or None
                session.avatar = info.get('avatar') or None
                save_login_session(storage, session)
                with self._lock:
                    self._status = 'success'
                    self._message = 'Login success'
                    self._uuid_cookie = None
                    self._qrcode = None
                    self._updated_at = utc_now_iso()
                return self._snapshot()
        except ApiError:
            raise
        except Exception as exc:
            with self._lock:
                self._status = 'error'
                self._message = str(exc)
                self._updated_at = utc_now_iso()
            raise ApiError(str(exc)) from exc

    def cancel(self) -> None:
        with self._lock:
            self._uuid_cookie = None
            self._qrcode = None
            self._status = 'idle'
            self._message = ''
            self._updated_at = utc_now_iso()


def _list_groups(storage: PostgresStorage) -> list[dict[str, Any]]:
    return [g.model_dump() for g in storage.groups.list_groups()]


def _get_group(storage: PostgresStorage, group_id: int) -> dict[str, Any]:
    try:
        group = storage.groups.get_group(group_id)
    except LookupError:
        raise ApiError('Group not found', status=404)
    return group.model_dump()


def _update_group(storage: PostgresStorage, group_id: int, updates: dict[str, Any]) -> dict[str, Any]:
    if not updates:
        raise ApiError('No fields to update')
    try:
        group = storage.groups.update_group(group_id, **updates)
    except LookupError:
        raise ApiError('Group not found', status=404)
    except ValueError as exc:
        raise ApiError(str(exc))
    return group.model_dump()


def _delete_group(storage: PostgresStorage, group_id: int) -> None:
    default_group = ensure_default_group(storage, name=DEFAULT_GROUP_NAME)
    default_id = default_group.id
    try:
        storage.groups.delete_group(group_id, default_id)
    except LookupError:
        raise ApiError('Group not found', status=404)
    except ValueError as exc:
        raise ApiError(str(exc), status=400)


def _list_accounts(
    storage: PostgresStorage,
    *,
    group_id: int | None,
    query: str | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    search_tokens: list[str] | None = None
    if query:
        tokens = _tokenize_query(query)
        if tokens:
            search_tokens = tokens
    return storage.accounts.list_accounts_paginated(
        group_id=group_id,
        search_tokens=search_tokens,
        page=page,
        page_size=page_size,
    )


def _get_account(storage: PostgresStorage, biz: str) -> dict[str, Any]:
    try:
        return _normalize_account_payload(storage.accounts.get_account_detail(biz))
    except LookupError:
        raise ApiError('Account not found', status=404)


def _normalize_account_payload(account: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(account)
    normalized['alias'] = normalized.get('alias') or ''
    return normalized


def _update_account(storage: PostgresStorage, biz: str, payload: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    mapping = {
        'nickname': 'nickname',
        'alias': 'alias',
        'round_head_img': 'round_head_img',
        'group_id': 'group_id',
        'is_disabled': 'is_disabled',
        'sync_mode': 'sync_mode',
        'sync_recent_days': 'sync_recent_days',
    }
    for key in mapping:
        if key in payload:
            value = payload[key]
            if key == 'is_disabled':
                value = bool(value)
            if key == 'sync_mode':
                value = _normalize_sync_mode(value)
            if key == 'sync_recent_days':
                value = _normalize_recent_days(value)
            updates[key] = value
    if not updates:
        raise ApiError('No fields to update')
    try:
        storage.accounts.update_account_fields(biz, **updates)
    except LookupError:
        raise ApiError('Account not found', status=404)
    return _get_account(storage, biz)


def _get_storage() -> Generator[PostgresStorage]:
    with open_storage() as storage:
        yield storage


def _get_login_manager(request: Request) -> LoginManager:
    return request.app.state.login_manager


def _get_sync_scheduler(request: Request) -> SyncScheduler | None:
    return getattr(request.app.state, 'sync_scheduler', None)


router = APIRouter(prefix='/api')


@router.get('/group')
def list_groups(storage: PostgresStorage = Depends(_get_storage)) -> dict[str, Any]:
    """
    获取所有分组列表。

    Returns:
        dict: 包含默认分组 ID 和所有分组列表的字典。
    """
    default_group = ensure_default_group(storage, name=DEFAULT_GROUP_NAME)
    return {
        'default_group_id': default_group.id,
        'groups': _list_groups(storage),
    }


@router.post('/group', status_code=status.HTTP_201_CREATED)
def create_group(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    创建一个新的分组。

    Args:
        body (dict): 请求体，包含分组名称 "name"。

    Returns:
        dict: 创建的分组 ID 和名称。

    Raises:
        ApiError: 如果分组名称缺失或为空。
    """
    name = str(body.get('name', '')).strip()
    if not name:
        raise ApiError('Group name is required')
    with storage.transaction():
        group = storage.groups.upsert_group(name)
    return {'id': group.id, 'name': group.name}


@router.get('/group/{group_id}')
def get_group(
    group_id: int,
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    获取指定分组的详细信息。

    Args:
        group_id (int): 分组 ID。

    Returns:
        dict: 分组详情，包括 ID、名称、同步设置和公众号数量。

    Raises:
        ApiError: 如果分组不存在。
    """
    return _get_group(storage, group_id)


@router.patch('/group/{group_id}')
def update_group(
    group_id: int,
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    更新指定分组的信息。

    Args:
        group_id (int): 分组 ID。
        body (dict): 需要更新的字段 (name, sync_mode, sync_recent_days)。

    Returns:
        dict: 更新后的分组详情。
    """
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


@router.delete('/group/{group_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_group(
    group_id: int,
    storage: PostgresStorage = Depends(_get_storage),
) -> Response:
    """
    删除指定分组。该分组下的公众号将被移动到默认分组。

    Args:
        group_id (int): 分组 ID。

    Returns:
        Response: HTTP 204 No Content。

    Raises:
        ApiError: 如果是默认分组或分组不存在。
    """
    _delete_group(storage, group_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get('/account/search')
async def search_account(
    q: str = '',
    page: int = 1,
    page_size: int = 10,
    begin: int | None = None,
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    通过微信接口搜索公众号。需要有效的登录会话。

    Args:
        q (str): 搜索关键词（公众号名称或 ID）。
        page (int): 页码 (默认: 1)。
        page_size (int): 每页结果数量 (默认: 10, 最大: 20)。
        begin (int | None): 可选的偏移量。

    Returns:
        dict: 包含公众号详情的搜索结果。

    Raises:
        ApiError: 如果关键词为空或会话过期。
    """
    keyword = (q or '').strip()
    if not keyword:
        raise ApiError('q is required')
    offset = begin if begin is not None else (max(page, 1) - 1) * page_size
    existing = {account.biz for account in storage.accounts.list_accounts()}
    session = storage.sessions.get_login_session()
    async with MPClient() as client:
        api_client = WeChatApiClient(client)
        try:
            payload = await api_client.search_biz(
                session,
                keyword=keyword,
                begin=offset,
                count=min(max(page_size, 1), 20),
            )
        except SessionExpiredError as exc:
            raise ApiError('Session expired. Please login again.', status=401) from exc
    records = payload.get('list') or []
    results: list[dict[str, Any]] = []
    for item in records:
        biz = (item.get('fakeid') or '').strip()
        if not biz:
            continue
        avatar_url = (item.get('round_head_img') or item.get('headimg') or '').strip()
        if avatar_url:
            _upsert_avatar_url(storage, biz, avatar_url)
        is_added = biz in existing
        results.append(
            {
                'biz': biz,
                'nickname': item.get('nickname') or '',
                'alias': item.get('alias') or '',
                'round_head_img': avatar_url,
                'is_added': is_added,
                'avatar_url': (f'/api/account/{biz}/avatar' if is_added else f'/api/account/search/{biz}/avatar'),
            }
        )
    return {
        'results': results,
        'page': max(page, 1),
        'page_size': min(max(page_size, 1), 20),
        'total': payload.get('total') or len(results),
    }


@router.get('/account/search/{biz}/avatar')
def get_search_avatar(
    biz: str,
    storage: PostgresStorage = Depends(_get_storage),
) -> Response:
    """
    获取搜索到的（尚未添加的）公众号头像。

    Args:
        biz (str): 公众号唯一标识 (fakeid)。

    Returns:
        Response: 包含正确 Content-Type 的图片数据。
    """
    avatar = _get_avatar_row(storage, biz)
    if not avatar:
        raise ApiError('Avatar not found', status=404)
    data = avatar.get('data')
    if not data:
        url = avatar.get('avatar_url')
        if url:
            cached = _fetch_and_cache_avatar(storage, biz, url)
            if cached:
                payload, content_type = cached
                return _binary_response(payload, content_type)
        raise ApiError('Avatar not found', status=404)
    payload = data.tobytes() if isinstance(data, memoryview) else bytes(data)
    content_type = avatar.get('content_type') or 'application/octet-stream'
    return _binary_response(payload, content_type)


@router.get('/account')
def list_accounts(
    group_id: int | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    列出已保存的公众号，支持筛选。

    Args:
        group_id (int | None): 按分组 ID 筛选。
        q (str | None): 按关键词搜索 (昵称、微信号或 biz)。
        page (int): 页码。
        page_size (int): 每页数量。

    Returns:
        dict: 公众号列表和分页信息。
    """
    payload = _list_accounts(
        storage,
        group_id=group_id,
        query=q or None,
        page=max(page, 1),
        page_size=min(max(page_size, 1), 200),
    )
    payload['accounts'] = [_normalize_account_payload(account) for account in payload.get('accounts', [])]
    return payload


@router.post('/account', status_code=status.HTTP_201_CREATED)
def create_account(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    添加一个新的公众号到数据库。

    Args:
        body (dict): 公众号详情，包括 biz, nickname 等。

    Returns:
        dict: 创建的公众号详情。

    Raises:
        ApiError: 如果缺少必填字段 (biz, nickname)。
    """
    required = ['biz', 'nickname']
    for field in required:
        if not body.get(field):
            raise ApiError(f'{field} is required')
    group_id = body.get('group_id')
    if group_id is None:
        default_group = ensure_default_group(storage, name=DEFAULT_GROUP_NAME)
        group_id = default_group.id
    sync_mode = _normalize_sync_mode(body.get('sync_mode'))
    sync_recent_days = _normalize_recent_days(body.get('sync_recent_days'))
    with storage.transaction():
        account = storage.accounts.upsert_account(
            AccountCredential(
                biz=str(body['biz']),
                nickname=str(body['nickname']),
                alias=body.get('alias'),
                round_head_img=body.get('round_head_img'),
                group_id=int(group_id) if group_id is not None else None,
                sync_mode=sync_mode,
                sync_recent_days=sync_recent_days,
            )
        )
    return _normalize_account_payload(
        {
            'biz': account.biz,
            'nickname': account.nickname,
            'alias': account.alias,
            'round_head_img': account.round_head_img,
            'group_id': account.group_id,
        }
    )


@router.post('/account/move')
def move_accounts(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    批量移动公众号到另一个分组。

    Args:
        body (dict): 包含 'biz_list' (字符串列表) 和 'group_id' (整数)。

    Returns:
        dict: 更新的公众号数量。
    """
    biz_list = body.get('biz_list') or []
    if not isinstance(biz_list, list) or not biz_list:
        raise ApiError('biz_list is required')
    group_id = body.get('group_id')
    if group_id is None:
        default_group = ensure_default_group(storage, name=DEFAULT_GROUP_NAME)
        group_id = default_group.id
    with storage.transaction():
        storage.accounts.move_accounts(biz_list, group_id)
    return {'updated': len(biz_list)}


@router.post('/account/batch')
def batch_update_accounts(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    批量更新多个公众号的同步设置。

    Args:
        body (dict): 包含 'biz_list' 和需更新的字段 ('sync_mode', 'sync_recent_days')。

    Returns:
        dict: 更新的公众号数量。
    """
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
    with storage.transaction():
        updated = storage.accounts.batch_update_fields(biz_list, **updates)
    return {'updated': updated}


@router.get('/account/{biz}')
def get_account(
    biz: str,
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    获取指定公众号的详细信息。

    Args:
        biz (str): 公众号唯一标识。

    Returns:
        dict: 公众号详情。

    Raises:
        ApiError: 如果公众号未找到。
    """
    return _get_account(storage, biz)


@router.patch('/account/{biz}')
def update_account(
    biz: str,
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    更新指定公众号的信息。

    Args:
        biz (str): 公众号唯一标识。
        body (dict): 需要更新的字段。

    Returns:
        dict: 更新后的公众号详情。
    """
    if 'group_id' in body and body['group_id'] is None:
        default_group = ensure_default_group(storage, name=DEFAULT_GROUP_NAME)
        body['group_id'] = default_group.id
    return _update_account(storage, biz, body)


@router.delete('/account/{biz}', status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    biz: str,
    storage: PostgresStorage = Depends(_get_storage),
) -> Response:
    """
    删除指定公众号及其关联的文章、图片和 S3 资源。

    Args:
        biz (str): 公众号唯一标识。

    Returns:
        Response: HTTP 204 No Content。

    Raises:
        ApiError: 如果公众号未找到。
    """
    s3_keys = storage.images.list_s3_keys_for_account(biz)
    if s3_keys:
        try:
            from .file_storage import S3FileStorage

            s3 = S3FileStorage()
            deleted = s3.delete_objects(s3_keys)
            logger.info('Deleted %d S3 objects for account %s', deleted, biz)
        except Exception as exc:
            logger.warning('S3 cleanup for account %s failed, deleting DB records anyway: %s', biz, exc)
    with storage.transaction():
        removed = storage.accounts.remove_account(biz)
    if removed == 0:
        raise ApiError('Account not found', status=404)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get('/account/{biz}/avatar')
def get_account_avatar(
    biz: str,
    storage: PostgresStorage = Depends(_get_storage),
) -> Response:
    """
    获取指定公众号的头像。

    Args:
        biz (str): 公众号唯一标识。

    Returns:
        Response: 包含正确 Content-Type 的图片数据。

    Raises:
        ApiError: 如果头像或公众号未找到。
    """
    avatar = _get_avatar_row(storage, biz)
    data = avatar.get('data') if avatar else None
    if not data:
        url = avatar.get('avatar_url') if avatar else None
        if not url:
            row = fetchone_row(
                storage,
                'SELECT round_head_img FROM accounts WHERE biz = %s',
                [biz],
                normalize=_normalize_record,
            )
            if not row:
                raise ApiError('Account not found', status=404)
            url = row.get('round_head_img')
            if url:
                _upsert_avatar_url(storage, biz, url)
        if url:
            cached = _fetch_and_cache_avatar(storage, biz, url)
            if cached:
                payload, content_type = cached
                return _binary_response(payload, content_type)
        raise ApiError('Avatar not found', status=404)
    payload = data.tobytes() if isinstance(data, memoryview) else bytes(data)
    content_type = avatar.get('content_type') or 'application/octet-stream'
    return _binary_response(payload, content_type)


@router.get('/article')
def list_articles(
    group_id: int | None = None,
    biz: str | None = None,
    item_show_type: int | None = None,
    article_id: str | None = None,
    q: str | None = None,
    exclude_keywords: str | None = None,
    sort: str | None = None,
    page: int = 1,
    page_size: int = 20,
    content: str = '',
    since: str | None = None,
    until: str | None = None,
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    列出文章，支持多种筛选条件。

    Args:
        group_id (int | None): 按分组 ID 筛选。
        biz (str | None): 按公众号 biz 筛选。
        article_id (str | None): 按具体文章 ID 筛选 (微信原始 article_id)。
        q (str | None): 搜索文章内容/标题。
        page (int): 页码。
        page_size (int): 每页数量。
        content (str): 如果为 "1", "true", 或 "yes"，则返回文章内容。
        since (str | None): 起始日期筛选 (ISO 格式)。
        until (str | None): 结束日期筛选 (ISO 格式)。

    Returns:
        dict: 文章列表和分页信息。
    """
    since_ts = _parse_date(since)
    until_ts = _parse_date(until, end_of_day=True)
    query_text = (q or '').strip()
    exclude_source = exclude_keywords
    if exclude_source is None:
        settings = load_sync_settings(storage)
        exclude_source = str(settings.get('article_exclude_keywords') or '')
    exclude_terms = _split_article_exclude_keywords(exclude_source)
    sort_mode = _normalize_article_sort(sort, has_query=bool(query_text))
    normalized_item_show_type = _normalize_item_show_type(item_show_type)
    return _list_articles(
        storage,
        group_id=group_id,
        biz=biz or None,
        item_show_type=normalized_item_show_type,
        query=query_text or None,
        exclude_keywords=exclude_terms or None,
        since_ts=since_ts,
        until_ts=until_ts,
        sort_mode=sort_mode,
        page=max(page, 1),
        page_size=min(max(page_size, 1), 200),
        article_id=article_id or None,
    )


@router.get('/article/{article_id}')
def get_article(
    article_id: int,
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    获取指定文章的完整详情。

    Args:
        article_id (int): 文章的主键 ID。

    Returns:
        dict: 文章详情，包括内容和图片。

    Raises:
        ApiError: 如果文章未找到。
    """
    return _get_article(storage, article_id)


@router.get('/article/{article_id}/image')
def list_article_images(
    article_id: int,
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    列出与文章关联的图片。

    Args:
        article_id (int): 文章 ID。

    Returns:
        dict: 图片元数据列表。
    """
    payload = _list_article_images(storage, article_id)
    return {'images': payload}


@router.get('/image/{image_id}')
def get_image(
    image_id: int,
    storage: PostgresStorage = Depends(_get_storage),
) -> Response:
    """
    通过 ID 获取图片内容。
    如果存储在 S3 中，则从 S3 获取。否则从源地址获取。

    Args:
        image_id (int): 图片 ID。

    Returns:
        Response: 图片二进制数据。
    """
    payload, content_type = _fetch_image(storage, image_id)
    return _binary_response(payload, content_type)


@router.post('/image/{image_id}/block')
def block_image(
    image_id: int,
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    Block an image globally by its binary content hash.

    Args:
        image_id (int): Image ID.

    Returns:
        dict: Blocking result and resolved hash.
    """
    return _block_image(storage, image_id)


_refetch_tasks: dict[str, dict[str, Any]] = {}
_refetch_lock = threading.Lock()


def _cleanup_refetch_tasks() -> None:
    now = time_module.monotonic()
    with _refetch_lock:
        stale = [
            tid
            for tid, t in _refetch_tasks.items()
            if t.get('status') in ('done', 'error') and now - t.get('finished_at', 0) > 300
        ]
        for tid in stale:
            del _refetch_tasks[tid]


@router.get('/article/refetch/{task_id}')
def get_refetch_status(task_id: str) -> dict[str, Any]:
    _cleanup_refetch_tasks()
    with _refetch_lock:
        task = _refetch_tasks.get(task_id)
    if not task:
        raise ApiError('Task not found', status=404)
    return dict(task)


@router.post('/article/{article_id}/refetch')
def refetch_article(
    article_id: int,
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    row = fetchone_row(
        storage,
        'SELECT link FROM articles WHERE id = %s',
        [article_id],
    )
    if not row:
        raise ApiError('Article not found', status=404)
    link = row.get('link')
    if not link:
        raise ApiError('Article has no source URL', status=400)

    task_id = uuid.uuid4().hex
    started_at = time_module.monotonic()
    with _refetch_lock:
        _refetch_tasks[task_id] = {
            'task_id': task_id,
            'status': 'running',
            'phase': 'downloading',
            'started_at': started_at,
            'error': None,
        }
    _cleanup_refetch_tasks()

    def _run() -> None:
        async def _do() -> None:
            with open_storage() as thread_storage:
                container = build_downloader_container(
                    storage=thread_storage,
                    enable_images=True,
                )
                async with container as app:
                    downloader = app.downloader
                    if not downloader:
                        raise RuntimeError('Downloader not initialized')
                    await downloader.download_from_url(str(link), with_images=True)
            with _refetch_lock:
                _refetch_tasks[task_id]['status'] = 'done'
                _refetch_tasks[task_id]['phase'] = 'done'
                _refetch_tasks[task_id]['finished_at'] = time_module.monotonic()

        try:
            asyncio.run(_do())
        except Exception as exc:
            logger.warning('Article refetch failed (task=%s): %s', task_id, exc)
            with _refetch_lock:
                _refetch_tasks[task_id]['status'] = 'error'
                _refetch_tasks[task_id]['phase'] = 'error'
                _refetch_tasks[task_id]['error'] = str(exc)
                _refetch_tasks[task_id]['finished_at'] = time_module.monotonic()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return {'task_id': task_id, 'status': 'started'}


@router.get('/login')
def login_status(
    storage: PostgresStorage = Depends(_get_storage),
    manager: LoginManager = Depends(_get_login_manager),
) -> dict[str, Any]:
    """
    获取当前的登录会话状态。

    Returns:
        dict: 登录状态，消息和上次登录信息。
    """
    info = _get_login_info(storage)
    return _login_response(manager._snapshot(), info)


@router.post('/login/start')
async def login_start(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(_get_storage),
    manager: LoginManager = Depends(_get_login_manager),
) -> dict[str, Any]:
    """
    开始新的登录会话（请求二维码）。

    Returns:
        dict: 登录状态，包含二维码 URL 是否可用。
    """
    force = bool(body.get('force'))
    info = _get_login_info(storage)
    return _login_response(await manager.start(force=force), info)


@router.post('/login/poll')
async def login_poll(
    storage: PostgresStorage = Depends(_get_storage),
    manager: LoginManager = Depends(_get_login_manager),
) -> dict[str, Any]:
    """
    轮询登录状态。应在开始登录后重复调用。
    检查二维码是否已被扫描或确认。

    Returns:
        dict: 更新后的登录状态。
    """
    info = _get_login_info(storage)
    return _login_response(await manager.poll(storage), info)


@router.post('/login/finalize')
async def login_finalize(
    storage: PostgresStorage = Depends(_get_storage),
    manager: LoginManager = Depends(_get_login_manager),
) -> dict[str, Any]:
    """
    完成登录（在二维码被确认后调用）。
    调用微信 bizlogin 接口完成最终登录，获取 token。
    """
    info = _get_login_info(storage)
    return _login_response(await manager.finalize(storage), info)


@router.post('/login/cancel')
def login_cancel(
    storage: PostgresStorage = Depends(_get_storage),
    manager: LoginManager = Depends(_get_login_manager),
) -> dict[str, Any]:
    """
    取消当前的登录尝试。

    Returns:
        dict: 重置后的登录状态。
    """
    manager.cancel()
    info = _get_login_info(storage)
    return _login_response(manager._snapshot(), info)


@router.get('/login/qrcode')
def login_qrcode(
    manager: LoginManager = Depends(_get_login_manager),
) -> Response:
    """
    获取登录二维码图片。

    Returns:
        Response: 二维码的 PNG 图片。
    """
    data = manager.get_qrcode()
    return Response(content=data, media_type='image/png')


@router.get('/settings/status')
def sync_status(
    limit: int = 5,
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    获取后台同步任务的状态。

    Returns:
        dict: 同步状态详情。
    """
    payload = load_sync_status(storage)
    history = payload.get('history')
    if isinstance(history, list):
        normalized_limit = min(max(int(limit), 1), 50)
        payload['history'] = history[:normalized_limit]
    return payload


@router.get('/settings/tasks')
def list_sync_tasks(
    limit: int = 5,
    detail: bool = False,
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    获取同步任务列表。
    """
    tasks = storage.sync_jobs.list_jobs(limit=max(int(limit), 1))
    formatter = (lambda task: task.to_dict()) if detail else (lambda task: task.to_summary_dict())
    return {
        'tasks': [formatter(task) for task in tasks],
    }


@router.get('/settings/tasks/{task_id}')
def get_sync_task(
    task_id: str,
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    获取指定同步任务的进度与状态。
    """
    state = storage.sync_jobs.get_job(task_id)
    if not state:
        raise ApiError('Task not found', status=404)
    return state.to_dict()


@router.post('/settings/tasks/{task_id}/cancel')
def cancel_sync_task(
    task_id: str,
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """Cancel a running or queued sync task."""
    with storage.transaction():
        cancelled = storage.sync_jobs.cancel_job(task_id)
    if not cancelled:
        raise ApiError('Task not found or not in a cancellable state', status=404)
    request_sync_cancel()
    state = storage.sync_jobs.get_job(task_id)
    return state.to_dict() if state else {}


@router.get('/settings')
def get_sync_settings(storage: PostgresStorage = Depends(_get_storage)) -> dict[str, Any]:
    """
    获取当前的同步配置设置。

    Returns:
        dict: 同步设置，包括启用状态、间隔、邮件配置等。
    """
    payload = load_sync_settings(storage)
    payload['email'] = get_email_settings(storage)
    return payload


@router.patch('/settings')
async def update_sync_settings(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(_get_storage),
    scheduler: SyncScheduler | None = Depends(_get_sync_scheduler),
) -> dict[str, Any]:
    """
    更新同步设置。

    Args:
        body (dict): 需要更新的设置 (enabled, interval_minutes, email 等)。

    Returns:
        dict: 更新后的设置。
    """
    updates: dict[str, Any] = {}
    if 'enabled' in body:
        updates['enabled'] = bool(body['enabled'])
    if 'interval_minutes' in body:
        updates['interval_minutes'] = max(int(body['interval_minutes']), 1)
    if 'window_start_hour' in body:
        updates['window_start_hour'] = min(max(int(body['window_start_hour']), 0), 23)
    if 'window_end_hour' in body:
        updates['window_end_hour'] = min(max(int(body['window_end_hour']), 0), 24)
    if 'sleep_seconds' in body:
        updates['sleep_seconds'] = float(body['sleep_seconds'])
    if 'download_content' in body:
        updates['download_content'] = bool(body['download_content'])
    if 'download_images' in body:
        updates['download_images'] = bool(body['download_images'])
    if 'skip_minutes' in body:
        updates['skip_minutes'] = max(int(body['skip_minutes']), 0)
    if 'article_exclude_keywords' in body:
        updates['article_exclude_keywords'] = str(body['article_exclude_keywords'] or '')
    if 'alert_enabled' in body:
        updates['alert_enabled'] = bool(body['alert_enabled'])
    if 'alert_email' in body:
        updates['alert_email'] = str(body['alert_email']).strip()
    settings = save_sync_settings(storage, updates)
    email_updates: dict[str, Any] = {}
    email_body = body.get('email')
    if isinstance(email_body, dict):
        for key in (
            'smtp_host',
            'smtp_port',
            'smtp_user',
            'smtp_password',
            'smtp_tls',
            'from_email',
        ):
            if key in email_body:
                email_updates[key] = email_body[key]
    if email_updates:
        settings['email'] = set_email_settings(storage, email_updates)
    else:
        settings['email'] = get_email_settings(storage)
    if settings.get('enabled') and scheduler:
        scheduler.trigger()
    return settings


@router.post('/settings/test-email')
async def send_sync_test_email(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    发送测试邮件，使用当前或传入的 SMTP 配置。
    """
    to_email = str(body.get('to_email') or '').strip()
    if not to_email:
        settings = load_sync_settings(storage)
        to_email = str(settings.get('alert_email') or '').strip()
    if not to_email:
        raise ApiError('to_email is required')

    email_settings = get_email_settings(storage)
    email_body = body.get('email')
    if isinstance(email_body, dict):
        if 'smtp_host' in email_body:
            email_settings['smtp_host'] = str(email_body.get('smtp_host') or '').strip()
        if 'smtp_port' in email_body:
            try:
                email_settings['smtp_port'] = max(int(email_body.get('smtp_port')), 1)
            except (TypeError, ValueError) as exc:
                raise ApiError('Invalid smtp_port') from exc
        if 'smtp_user' in email_body:
            email_settings['smtp_user'] = str(email_body.get('smtp_user') or '').strip()
        if 'smtp_password' in email_body:
            email_settings['smtp_password'] = str(email_body.get('smtp_password') or '')
        if 'smtp_tls' in email_body:
            email_settings['smtp_tls'] = bool(email_body.get('smtp_tls'))
        if 'from_email' in email_body:
            email_settings['from_email'] = str(email_body.get('from_email') or '').strip()

    smtp_host = str(email_settings.get('smtp_host') or '').strip()
    if not smtp_host:
        raise ApiError('smtp_host is required')

    sent_at = datetime.now(UTC).isoformat()
    subject = 'Hippo test email'
    message = f'This is a test email from Hippo.\n\nSent at (UTC): {sent_at}\nTo: {to_email}\n'
    started_at = time_module.monotonic()
    logger.info(
        'Sending test email: to=%s smtp_host=%s smtp_port=%s smtp_tls=%s',
        to_email,
        smtp_host,
        email_settings.get('smtp_port'),
        bool(email_settings.get('smtp_tls')),
    )
    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                send_email,
                email_settings,
                to_email=to_email,
                subject=subject,
                body=message,
            ),
            timeout=20,
        )
    except TimeoutError as exc:
        elapsed_ms = int((time_module.monotonic() - started_at) * 1000)
        logger.warning(
            'Test email timeout: to=%s smtp_host=%s elapsed_ms=%s',
            to_email,
            smtp_host,
            elapsed_ms,
        )
        raise ApiError('Test email timed out. Please verify SMTP host/port/TLS.') from exc
    except Exception as exc:
        elapsed_ms = int((time_module.monotonic() - started_at) * 1000)
        logger.warning(
            'Failed to send test email: to=%s smtp_host=%s elapsed_ms=%s error=%s',
            to_email,
            smtp_host,
            elapsed_ms,
            exc,
        )
        raise ApiError(f'Failed to send test email: {exc}') from exc
    elapsed_ms = int((time_module.monotonic() - started_at) * 1000)
    logger.info(
        'Test email sent: to=%s smtp_host=%s elapsed_ms=%s',
        to_email,
        smtp_host,
        elapsed_ms,
    )

    return {'status': 'sent', 'to_email': to_email}


@router.post('/settings/run', status_code=status.HTTP_202_ACCEPTED)
async def run_sync(
    body: dict[str, Any] = Body(default={}),
    storage: PostgresStorage = Depends(_get_storage),
) -> dict[str, Any]:
    """
    手动触发同步操作。

    Args:
        body (dict): 可选的 'group_id' 用于同步指定分组。

    Returns:
        dict: 触发操作的状态。
    """
    group_id = body.get('group_id')
    raw_biz_list = body.get('biz_list')
    biz_list: list[str] | None = None
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
    if raw_biz_list is not None:
        if not isinstance(raw_biz_list, list):
            raise ApiError('Invalid biz_list')
        normalized_biz_list: list[str] = []
        seen_biz: set[str] = set()
        for value in raw_biz_list:
            biz = str(value or '').strip()
            if not biz or biz in seen_biz:
                continue
            normalized_biz_list.append(biz)
            seen_biz.add(biz)
        if not normalized_biz_list:
            raise ApiError('Invalid biz_list')
        known_biz = {account.biz for account in storage.accounts.list_accounts()}
        missing_biz = [biz for biz in normalized_biz_list if biz not in known_biz]
        if missing_biz:
            raise ApiError(f'Account not found: {missing_biz[0]}', status=404)
        biz_list = normalized_biz_list
    with storage.transaction():
        task_state = storage.sync_jobs.create_job(
            trigger_type='manual',
            group_id=group_id,
            biz_list=biz_list,
        )
    response: dict[str, Any] = {
        'status': task_state.status,
        'task_id': task_state.task_id,
    }
    if group_id is not None:
        response['group_id'] = group_id
    if biz_list is not None:
        response['biz_list'] = biz_list
    return response


@router.get('/feed/mixed', response_model=None)
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
    """
    获取所有公众号或特定分组的混合文章流。
    支持 JSON 和 RSS 输出。

    Args:
        request (Request): 请求对象。
        group_id (int | None): 按分组 ID 筛选。
        biz (str | None): 按公众号 biz 筛选。
        q (str | None): 搜索关键词。
        limit (int): 返回条目数量 (默认: 50)。
        format (str | None): 输出格式 ("rss" 表示 RSS Feed)。
        since (str | None): 起始日期筛选。
        until (str | None): 结束日期筛选。
        days (int | None): 按最近天数筛选。

    Returns:
        dict | Response: 文章列表或 RSS XML 响应。
    """
    output_format = (format or '').lower()
    since_ts = _parse_date(since)
    until_ts = _parse_date(until, end_of_day=True)
    if days:
        now = datetime.utcnow()
        since_ts = int(now.timestamp() - days * 86400)
    if output_format == 'rss':
        group_names: list[str] = []
        if group_id is not None:
            row = fetchone_row(
                storage,
                'SELECT name FROM account_groups WHERE id = %s',
                [group_id],
                normalize=_normalize_record,
            )
            if not row:
                raise ApiError('Group not found', status=404)
            group_names = [row.get('name') or '']
        host = request.headers.get('host') or f'{DEFAULT_HOST}:{DEFAULT_PORT}'
        scheme = request.url.scheme or 'http'
        image_base = f'{scheme}://{host}'
        items = query_rss_items(
            group_names=group_names,
            limit=min(max(limit, 1), 500),
            days=days,
            since=since,
            until=until,
            image_base_url=image_base,
        )
        title = 'Hippo RSS'
        description = 'Hippo RSS feed'
        if group_names:
            title = f'{group_names[0]} - Hippo RSS'
            description = f'RSS feed for {group_names[0]}'
        xml = build_rss_xml(
            title=title,
            link=image_base,
            description=description,
            items=items,
        )
        return Response(
            content=xml.encode('utf-8'),
            media_type='application/rss+xml; charset=utf-8',
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
    return {'articles': payload}


def create_app(
    static_dir: Path | str = 'frontend/dist',
    *,
    enable_inprocess_sync: bool | None = None,
) -> FastAPI:
    log_level, _ = _resolve_log_level()
    logging.basicConfig(level=log_level, format='%(levelname)s: %(message)s', force=True)
    static_path = Path(static_dir).expanduser().resolve()
    if not static_path.exists():
        raise RuntimeError(f'Static directory not found: {static_path}. Run `npm --prefix frontend build` first.')

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        with open_storage() as storage:
            ensure_default_group(storage, name=DEFAULT_GROUP_NAME)
            _ensure_avatar_images_table(storage)
        app.state.login_manager = LoginManager()
        should_enable_sync = _inprocess_sync_enabled() if enable_inprocess_sync is None else enable_inprocess_sync
        if should_enable_sync:
            app.state.sync_scheduler = SyncScheduler()
            app.state.sync_scheduler.start()
        else:
            app.state.sync_scheduler = None
        try:
            yield
        finally:
            scheduler = getattr(app.state, 'sync_scheduler', None)
            if scheduler:
                await scheduler.stop()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=['*'],
        allow_methods=['*'],
        allow_headers=['*'],
    )

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        logger.warning(
            'API error: path=%s method=%s status=%s message=%s',
            request.url.path,
            request.method,
            exc.status,
            str(exc),
        )
        return JSONResponse(status_code=exc.status, content={'error': str(exc)})

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception('Unexpected error: %s %s', request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={'error': 'Internal server error'},
        )

    app.include_router(router)
    app.mount('/', StaticFiles(directory=static_path, html=True), name='static')
    return app


def serve(
    host: str | None = DEFAULT_HOST,
    port: int | None = DEFAULT_PORT,
    static_dir: Path | str = 'frontend/dist',
    *,
    unix_socket: Path | str | None = None,
    unix_socket_mode: int = 0o660,
    enable_inprocess_sync: bool | None = None,
) -> None:
    import uvicorn

    app = create_app(static_dir=static_dir, enable_inprocess_sync=enable_inprocess_sync)
    _, uvicorn_log_level = _resolve_log_level()
    listen_sockets = _build_listen_sockets(
        host=host,
        port=port,
        unix_socket=unix_socket,
        unix_socket_mode=unix_socket_mode,
    )
    config = uvicorn.Config(
        app,
        host=host or DEFAULT_HOST,
        port=DEFAULT_PORT if port is None else port,
        log_level=uvicorn_log_level,
    )
    server = uvicorn.Server(config)
    try:
        server.run(sockets=listen_sockets)
    finally:
        for candidate in listen_sockets:
            candidate.close()
