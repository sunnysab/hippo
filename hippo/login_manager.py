"""WeRead credential management for the web UI."""

from __future__ import annotations

import threading
from typing import Any

from .exceptions import ApiError
from .http import MPClient
from .storage import PostgresStorage
from .utils import utc_now_iso
from .wechat_api import SessionExpiredError, WeChatApiClient


def _clear_login_required(storage: PostgresStorage) -> None:
    with storage.transaction():
        storage.meta.delete('sync:login_required_at')
        storage.meta.delete('sync:alert_sent')
        storage.meta.set('sync:last_error', '')
        if storage.meta.get('sync:last_status') == 'login_required':
            storage.meta.set('sync:last_status', 'idle')


def _mark_login_required(storage: PostgresStorage, message: str) -> None:
    finished_at = utc_now_iso()
    with storage.transaction():
        storage.meta.set('sync:login_required_at', finished_at)
        storage.meta.set('sync:last_error', message)
        if storage.meta.get('sync:last_status') != 'login_required':
            storage.meta.set('sync:last_status', 'login_required')


class LoginManager:
    """Tracks the imported WeRead credential and exposes refresh/clear actions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_error: str | None = None

    def snapshot(self, storage: PostgresStorage) -> dict[str, Any]:
        try:
            session = storage.sessions.get_login_session()
        except LookupError:
            return {
                'status': 'missing',
                'message': 'No WeRead credential imported.',
                'has_credential': False,
                'vid': None,
                'nickname': None,
                'avatar': None,
                'updated_at': None,
                'last_error': self._last_error,
            }
        updated_at = storage.sessions.get_login_updated_at()
        return {
            'status': 'error' if self._last_error else 'ok',
            'message': self._last_error or '',
            'has_credential': True,
            'vid': session.vid,
            'nickname': session.nickname,
            'avatar': session.avatar,
            'updated_at': updated_at.isoformat() if updated_at else None,
            'last_error': self._last_error,
        }

    async def refresh(self, storage: PostgresStorage) -> dict[str, Any]:
        with self._lock:
            try:
                session = storage.sessions.get_login_session()
            except LookupError as exc:
                self._last_error = str(exc)
                raise ApiError('No WeRead credential to refresh', status=400) from exc
            async with MPClient(timeout=15.0) as client:
                api_client = WeChatApiClient(client, storage=storage)
                try:
                    await api_client._refresh(session)
                except SessionExpiredError as exc:
                    self._last_error = str(exc)
                    _mark_login_required(storage, str(exc))
                    raise ApiError(str(exc), status=401) from exc
            self._last_error = None
            _clear_login_required(storage)
            return self.snapshot(storage)

    def clear(self, storage: PostgresStorage) -> dict[str, Any]:
        with self._lock:
            with storage.transaction():
                storage.sessions.clear_sessions()
            self._last_error = None
        return self.snapshot(storage)

    def mark_imported(self, storage: PostgresStorage) -> dict[str, Any]:
        with self._lock:
            self._last_error = None
            _clear_login_required(storage)
            return self.snapshot(storage)


__all__ = ['LoginManager']
