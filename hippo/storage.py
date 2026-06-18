"""PostgreSQL-backed persistence for the CLI."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, suppress
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .env import load_env
from .models import AccountGroup
from .repositories import (
    AccountRepository,
    ArticleRepository,
    GroupRepository,
    ImageRepository,
    LoginSessionRepository,
    MetaRepository,
)
from .sync_jobs import SyncJobRepository

SCHEMA_VERSION = '18'

SCHEMA_PATH = Path(__file__).resolve().parent.parent / 'schema' / 'postgres.sql'


UPSERT_SCHEMA_VERSION = """
INSERT INTO meta(key, value)
VALUES ('schema_version', %s)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
"""

_PG_POOL: ConnectionPool | None = None
_PG_POOL_DSN: str | None = None
_DB_INIT_LOG_VALUES = {'1', 'true', 'yes', 'on'}
_PG_JIEBA_WARMUP_VALUES = {'1', 'true', 'yes', 'on'}
_PG_DISABLE_JIT_VALUES = {'1', 'true', 'yes', 'on'}
_DEFAULT_JIEBA_WARMUP_TEXT = 'hippo'


def _db_init_log_enabled() -> bool:
    return os.environ.get('HIPPO_DB_INIT_LOG', '').strip().lower() in _DB_INIT_LOG_VALUES


def _log_db_init(message: str) -> None:
    if not _db_init_log_enabled():
        return
    print(f'[db init] {message}', file=sys.stderr, flush=True)


def _jieba_warmup_enabled() -> bool:
    return os.environ.get('HIPPO_PG_JIEBA_WARMUP', '1').strip().lower() in _PG_JIEBA_WARMUP_VALUES


def _pg_disable_jit_enabled() -> bool:
    return os.environ.get('HIPPO_PG_DISABLE_JIT', '1').strip().lower() in _PG_DISABLE_JIT_VALUES


def _rollback_quietly(conn) -> None:
    with suppress(Exception):
        conn.rollback()


def _warmup_jieba_parser(conn) -> None:
    if not _jieba_warmup_enabled():
        return
    warmup_text = os.environ.get('HIPPO_PG_JIEBA_WARMUP_TEXT', _DEFAULT_JIEBA_WARMUP_TEXT).strip()
    if not warmup_text:
        warmup_text = _DEFAULT_JIEBA_WARMUP_TEXT
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT plainto_tsquery('jiebaqry', %s)", (warmup_text,))
            cur.fetchone()
    except Exception as exc:
        _log_db_init(f'jieba warmup skipped: {exc}')
    finally:
        _rollback_quietly(conn)


def _get_pool(dsn: str) -> ConnectionPool:
    global _PG_POOL, _PG_POOL_DSN
    if _PG_POOL is not None and dsn == _PG_POOL_DSN:
        return _PG_POOL
    if _PG_POOL is not None:
        _PG_POOL.close()
    min_conn = int(os.environ.get('HIPPO_PG_POOL_MIN', '1') or '1')
    max_conn = int(os.environ.get('HIPPO_PG_POOL_MAX', '8') or '8')
    if max_conn < min_conn:
        max_conn = min_conn
    options = ['-c timezone=Asia/Shanghai']
    if _pg_disable_jit_enabled():
        options.append('-c jit=off')
    _PG_POOL = ConnectionPool(
        conninfo=dsn,
        min_size=min_conn,
        max_size=max_conn,
        open=True,
        kwargs={'options': ' '.join(options)},
        configure=_warmup_jieba_parser,
    )
    _PG_POOL_DSN = dsn
    return _PG_POOL


class StorageInitError(RuntimeError):
    pass


def _load_schema_sql() -> str:
    try:
        return SCHEMA_PATH.read_text(encoding='utf-8')
    except FileNotFoundError as exc:
        raise StorageInitError(f'Schema file not found: {SCHEMA_PATH}') from exc


class PostgresStorage(AbstractContextManager):
    def __init__(
        self,
        dsn: str,
        *,
        auto_init: bool = False,
        pool: ConnectionPool | None = None,
    ) -> None:
        self.dsn = dsn
        self._pool = pool or _get_pool(dsn)
        self.conn = self._pool.getconn()
        self.conn.autocommit = False
        if auto_init:
            self._init_db()
        else:
            try:
                self._ensure_initialized()
            except Exception:
                self.close()
                raise
        self.meta = MetaRepository(self.conn)
        self.groups = GroupRepository(self.conn)
        self.accounts = AccountRepository(self.conn, group_repo=self.groups)
        self.sessions = LoginSessionRepository(self.conn)
        self.articles = ArticleRepository(self.conn)
        self.images = ImageRepository(self.conn)
        self.sync_jobs = SyncJobRepository(self.conn)

    def __enter__(self) -> PostgresStorage:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        self.close()

    def close(self) -> None:
        if self._pool:
            with suppress(Exception):
                self.conn.rollback()
            self._pool.putconn(self.conn)
        else:
            self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def transaction(self) -> StorageTransaction:
        return StorageTransaction(self.conn)

    def _init_db(self) -> None:
        with self.conn.cursor() as cur:
            schema_sql = _load_schema_sql()
            first_line = schema_sql.strip().splitlines()[0].strip()[:160]
            _log_db_init(
                f'executing schema from {SCHEMA_PATH.name} ({len(schema_sql)} bytes, starts with: {first_line}...)'
            )
            cur.execute(schema_sql)
            _log_db_init('update schema version')
            cur.execute(UPSERT_SCHEMA_VERSION, (SCHEMA_VERSION,))
        self.conn.commit()

    def _ensure_initialized(self) -> None:
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.meta')")
                table_name = cur.fetchone()[0]
                if not table_name:
                    raise StorageInitError('Database not initialized. Run `python -m hippo db init`.')
                cur.execute('SELECT value FROM meta WHERE key = %s', ('schema_version',))
                row = cur.fetchone()
                if not row:
                    raise StorageInitError('Database not initialized. Run `python -m hippo db init`.')
                current_version = row[0]
                if current_version != SCHEMA_VERSION:
                    raise StorageInitError('Database schema out of date. Run `python -m hippo db init` to migrate.')
            self.conn.rollback()
        except Exception:
            self.conn.rollback()
            raise


def open_storage(*, auto_init: bool = False) -> PostgresStorage:
    load_env()
    dsn = os.environ.get('HIPPO_PG_DSN')
    if not dsn:
        raise StorageInitError('Missing HIPPO_PG_DSN for PostgreSQL storage.')
    return PostgresStorage(dsn, auto_init=auto_init)


class StorageTransaction(AbstractContextManager):
    def __init__(self, conn) -> None:
        self._conn = conn

    def __enter__(self) -> StorageTransaction:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        if exc_type:
            self._conn.rollback()
        else:
            self._conn.commit()


def fetchall_rows(
    storage: PostgresStorage,
    query: str,
    params: Sequence[Any],
    *,
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    with storage.conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    if normalize:
        return [normalize(dict(row)) for row in rows]
    return [dict(row) for row in rows]


def fetchone_row(
    storage: PostgresStorage,
    query: str,
    params: Sequence[Any],
    *,
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    with storage.conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    if not row:
        return None
    record = dict(row)
    return normalize(record) if normalize else record


def load_meta_json(storage: PostgresStorage, key: str, default: Any) -> Any:
    raw = storage.meta.get(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def save_meta_json(storage: PostgresStorage, key: str, value: Any) -> None:
    storage.meta.set(key, json.dumps(value, ensure_ascii=False))


def ensure_default_group(storage: PostgresStorage, *, name: str = 'Default') -> AccountGroup:
    with storage.transaction():
        default_group = storage.groups.upsert_group(name)
        default_id = default_group.id
        with storage.conn.cursor() as cur:
            cur.execute(
                'UPDATE accounts SET group_id = %s WHERE group_id IS NULL',
                (default_id,),
            )
    return default_group


__all__ = [
    'PostgresStorage',
    'StorageInitError',
    'StorageTransaction',
    'ensure_default_group',
    'fetchall_rows',
    'fetchone_row',
    'load_meta_json',
    'open_storage',
    'save_meta_json',
]
