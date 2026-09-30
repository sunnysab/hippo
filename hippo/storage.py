"""PostgreSQL-backed persistence for the CLI."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager, suppress
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .exceptions import StorageInitError
from .models import AccountGroup
from .repositories import (
    AccountRepository,
    ArticleDocumentRepository,
    ArticleQueueRepository,
    ArticleRepository,
    AuditRepository,
    DownloadAttemptRepository,
    GroupRepository,
    ImageRepository,
    MetaRepository,
    SubscriptionRepository,
    UserRepository,
    UserSessionRepository,
    UserTokenRepository,
)
from .sync_jobs import SyncJobRepository

SCHEMA_VERSION = '21'

SCHEMA_PATH = Path(__file__).resolve().parent.parent / 'schema' / 'postgres.sql'


UPSERT_SCHEMA_VERSION = """
INSERT INTO meta(key, value)
VALUES ('schema_version', %s)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
"""

_pool_local = threading.local()
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


def _extract_error_snippet(sql: str, exc: psycopg.errors.DatabaseError) -> str:
    position = getattr(exc.diag, 'position', None)
    if not position:
        return ''
    pos = int(position)
    start = max(sql.rfind('\n', 0, pos), 0)
    end = sql.find('\n', pos)
    if end == -1:
        end = len(sql)
    line = sql[start:end].strip()
    if len(line) > 200:
        line = line[:200] + '...'
    return f'at position {pos}: "{line}"'


def _jieba_warmup_enabled() -> bool:
    return os.environ.get('HIPPO_PG_JIEBA_WARMUP', '1').strip().lower() in _PG_JIEBA_WARMUP_VALUES


def _pg_disable_jit_enabled() -> bool:
    return os.environ.get('HIPPO_PG_DISABLE_JIT', '1').strip().lower() in _PG_DISABLE_JIT_VALUES


async def _rollback_quietly(conn) -> None:
    with suppress(Exception):
        await conn.rollback()


async def _warmup_jieba_parser(conn) -> None:
    if not _jieba_warmup_enabled():
        return
    warmup_text = os.environ.get('HIPPO_PG_JIEBA_WARMUP_TEXT', _DEFAULT_JIEBA_WARMUP_TEXT).strip()
    if not warmup_text:
        warmup_text = _DEFAULT_JIEBA_WARMUP_TEXT
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT plainto_tsquery('jiebaqry', %s)", (warmup_text,))
            await cur.fetchone()
    except Exception as exc:
        _log_db_init(f'jieba warmup skipped: {exc}')
    finally:
        await _rollback_quietly(conn)


async def get_pool(dsn: str) -> AsyncConnectionPool:
    """Return the connection pool for ``dsn`` in the current thread and loop.

    The pool is bound to the running event loop, so a new loop (each CLI
    command runs its own ``asyncio.run``) transparently replaces it. Keeping
    it thread-local matters for the multi-worker backfill path, which runs
    one event loop per thread: a shared pool would be closed by whichever
    thread noticed the loop change first, starving the others.
    """
    loop = asyncio.get_running_loop()
    cached = getattr(_pool_local, 'pool', None)
    if cached is not None and getattr(_pool_local, 'dsn', None) == dsn and getattr(_pool_local, 'loop', None) is loop:
        return cached
    if cached is not None:
        with suppress(Exception):
            await cached.close()
    min_conn = int(os.environ.get('HIPPO_PG_POOL_MIN', '1') or '1')
    max_conn = int(os.environ.get('HIPPO_PG_POOL_MAX', '8') or '8')
    if max_conn < min_conn:
        max_conn = min_conn
    options = ['-c timezone=Asia/Shanghai']
    if _pg_disable_jit_enabled():
        options.append('-c jit=off')
    pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=min_conn,
        max_size=max_conn,
        open=False,
        kwargs={'options': ' '.join(options)},
        configure=_warmup_jieba_parser,
    )
    await pool.open()
    _pool_local.pool = pool
    _pool_local.dsn = dsn
    _pool_local.loop = loop
    return pool


async def close_pool() -> None:
    """Close the current thread's pool (used by long-running entrypoints)."""
    pool = getattr(_pool_local, 'pool', None)
    if pool is not None:
        with suppress(Exception):
            await pool.close()
    _pool_local.pool = None
    _pool_local.dsn = None
    _pool_local.loop = None


def _load_schema_sql() -> str:
    try:
        return SCHEMA_PATH.read_text(encoding='utf-8')
    except FileNotFoundError as exc:
        raise StorageInitError(f'Schema file not found: {SCHEMA_PATH}') from exc


class StorageTransaction(AbstractAsyncContextManager):
    """Explicit transaction block.

    Commits and rolls back the connection directly instead of going through
    ``Connection.transaction()``. psycopg degrades that helper into a SAVEPOINT
    when a transaction is already open — which it is as soon as any earlier
    query ran — and releasing a savepoint does not persist anything, while the
    connection close rolls the outer transaction back.
    """

    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> StorageTransaction:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        if exc_type:
            await self._conn.rollback()
        else:
            await self._conn.commit()


class PostgresStorage(AbstractAsyncContextManager):
    """Async storage handle. Use ``async with open_storage() as storage``."""

    def __init__(
        self,
        dsn: str,
        *,
        auto_init: bool = False,
        pool: AsyncConnectionPool | None = None,
    ) -> None:
        self.dsn = dsn
        self.auto_init = auto_init
        self._pool = pool
        self.conn: psycopg.AsyncConnection | None = None

    async def __aenter__(self) -> PostgresStorage:
        self._pool = self._pool or await get_pool(self.dsn)
        self.conn = await self._pool.getconn()
        await self.conn.set_autocommit(False)
        if self.auto_init:
            await self._init_db()
        else:
            try:
                await self._ensure_initialized()
            except Exception:
                await self.close()
                raise
        self._bind_repositories()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        await self.close()

    def _bind_repositories(self) -> None:
        self.meta = MetaRepository(self.conn)
        self.groups = GroupRepository(self.conn)
        self.accounts = AccountRepository(self.conn)
        self.articles = ArticleRepository(self.conn)
        self.documents = ArticleDocumentRepository(self.conn)
        self.article_queue = ArticleQueueRepository(self.conn)
        self.images = ImageRepository(self.conn)
        self.download_attempts = DownloadAttemptRepository(self.conn)
        self.sync_jobs = SyncJobRepository(self.conn)
        self.users = UserRepository(self.conn)
        self.sessions = UserSessionRepository(self.conn)
        self.tokens = UserTokenRepository(self.conn)
        self.subscriptions = SubscriptionRepository(self.conn)
        self.audit = AuditRepository(self.conn)

    async def close(self) -> None:
        if self.conn is None:
            return
        if self._pool:
            with suppress(Exception):
                await self.conn.rollback()
            await self._pool.putconn(self.conn)
        else:
            await self.conn.close()
        self.conn = None

    async def commit(self) -> None:
        await self.conn.commit()

    async def rollback(self) -> None:
        await self.conn.rollback()

    def transaction(self) -> StorageTransaction:
        return StorageTransaction(self.conn)

    async def _init_db(self) -> None:
        async with self.conn.cursor() as cur:
            schema_sql = _load_schema_sql()
            _log_db_init(f'executing schema from {SCHEMA_PATH.name} ({len(schema_sql)} bytes)')
            try:
                await cur.execute(schema_sql)
            except psycopg.errors.DatabaseError as exc:
                snippet = _extract_error_snippet(schema_sql, exc)
                _log_db_init(f'schema execution failed{f": {snippet}" if snippet else ""}')
                raise
            _log_db_init('update schema version')
            await cur.execute(UPSERT_SCHEMA_VERSION, (SCHEMA_VERSION,))
        await self.conn.commit()

    async def _ensure_initialized(self) -> None:
        try:
            async with self.conn.cursor() as cur:
                await cur.execute("SELECT to_regclass('public.meta')")
                table_name = (await cur.fetchone())[0]
                if not table_name:
                    raise StorageInitError('Database not initialized. Run `python -m hippo db init`.')
                await cur.execute('SELECT value FROM meta WHERE key = %s', ('schema_version',))
                row = await cur.fetchone()
                if not row:
                    raise StorageInitError('Database not initialized. Run `python -m hippo db init`.')
                current_version = row[0]
                if current_version != SCHEMA_VERSION:
                    raise StorageInitError('Database schema out of date. Run `python -m hippo db init` to migrate.')
            await self.conn.rollback()
        except Exception:
            await self.conn.rollback()
            raise


def open_storage(*, auto_init: bool = False) -> PostgresStorage:
    dsn = os.environ.get('HIPPO_PG_DSN')
    if not dsn:
        raise StorageInitError('Missing HIPPO_PG_DSN for PostgreSQL storage.')
    return PostgresStorage(dsn, auto_init=auto_init)


async def fetchall_rows(
    storage: PostgresStorage,
    query: str,
    params: Sequence[Any],
    *,
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    async with storage.conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        rows = await cur.fetchall()
    if normalize:
        return [normalize(dict(row)) for row in rows]
    return [dict(row) for row in rows]


async def fetchone_row(
    storage: PostgresStorage,
    query: str,
    params: Sequence[Any],
    *,
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    async with storage.conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        row = await cur.fetchone()
    if not row:
        return None
    record = dict(row)
    return normalize(record) if normalize else record


async def load_meta_json(storage: PostgresStorage, key: str, default: Any) -> Any:
    raw = await storage.meta.get(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


async def save_meta_json(storage: PostgresStorage, key: str, value: Any) -> None:
    await storage.meta.set(key, json.dumps(value, ensure_ascii=False))


async def ensure_default_group(
    storage: PostgresStorage,
    user_id: int,
    *,
    name: str = 'Default',
) -> AccountGroup:
    """Return the user's fallback group, creating it on first use.

    Groups are per-user now, so this needs the owner rather than touching the
    shared ``accounts`` table.
    """
    async with storage.transaction():
        return await storage.groups.upsert_group(name, user_id=user_id)


__all__ = [
    'PostgresStorage',
    'StorageInitError',
    'StorageTransaction',
    'close_pool',
    'ensure_default_group',
    'fetchall_rows',
    'fetchone_row',
    'get_pool',
    'load_meta_json',
    'open_storage',
    'save_meta_json',
]
