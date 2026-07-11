"""QR code login state machine for WeChat MP backend."""

from __future__ import annotations

import random
import threading
import time as time_module
from typing import Any

from .exceptions import ApiError
from .http import MPClient
from .login_service import save_login_session
from .storage import PostgresStorage
from .utils import utc_now_iso
from .wechat_api import WeChatApiClient


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
                # Clear login_required sync block since session is now refreshed
                with storage.transaction():
                    storage.meta.delete('sync:login_required_at')
                    storage.meta.delete('sync:alert_sent')
                    if storage.meta.get('sync:last_status') == 'login_required':
                        storage.meta.set('sync:last_status', 'idle')
                        storage.meta.set('sync:last_error', '')
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


__all__ = ['LoginManager']
