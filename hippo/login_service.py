"""Login session persistence helpers."""

from __future__ import annotations

from psycopg.errors import UniqueViolation

from .models import LoginSession
from .storage import PostgresStorage


def save_login_session(
    storage: PostgresStorage,
    session: LoginSession,
    *,
    set_default: bool = True,
) -> LoginSession:
    for attempt in range(2):
        try:
            with storage.transaction():
                return storage.sessions.save_login_session(session, set_default=set_default)
        except UniqueViolation:
            with storage.transaction():
                storage.sessions.reset_login_session_sequence()
            if attempt >= 1:
                raise
    return storage.sessions.get_login_session()


__all__ = ['save_login_session']
