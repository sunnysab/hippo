"""Login session persistence helpers."""

from __future__ import annotations

from pathlib import Path

from psycopg.errors import UniqueViolation

from .models import LoginSession
from .storage import PostgresStorage
from .weread_dump import load_credential


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


def import_weread_dump(
    storage: PostgresStorage,
    dump_dir: str | Path,
    *,
    vid: str | None = None,
) -> LoginSession:
    """Import a WeRead account dump into the default login session."""
    session = load_credential(dump_dir, vid=vid)
    return save_login_session(storage, session)


__all__ = ['import_weread_dump', 'save_login_session']
