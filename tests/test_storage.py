from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo import storage
from hippo.exceptions import StorageInitError


class _FakeCursor:
    """A cursor handing back the rows the initialization check reads."""

    def __init__(self, rows: list[object]) -> None:
        self._rows = iter(rows)

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def fetchone(self) -> object:
        return next(self._rows)


class SchemaVersionTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_stale_schema_version_refuses_to_open(self) -> None:
        # Startup compares meta.schema_version with the constant; a mismatch must
        # stop the process instead of running against a schema it cannot read.
        conn = SimpleNamespace(
            cursor=lambda: _FakeCursor([('meta',), ('0',)]),
            rollback=AsyncMock(),
        )
        handle = storage.PostgresStorage('postgresql://ignored')
        handle.conn = conn  # type: ignore[assignment]

        with self.assertRaises(StorageInitError) as ctx:
            await handle._ensure_initialized()

        self.assertIn('out of date', str(ctx.exception))


class StoragePoolTest(unittest.IsolatedAsyncioTestCase):
    async def test_get_pool_passes_explicit_open_flag(self) -> None:
        captured: dict[str, object] = {}

        class DummyPool:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)

            async def open(self) -> None:
                return None

            async def close(self) -> None:
                return None

        previous = (
            getattr(storage._pool_local, 'pool', None),
            getattr(storage._pool_local, 'dsn', None),
            getattr(storage._pool_local, 'loop', None),
        )
        storage._pool_local.pool = None
        storage._pool_local.dsn = None
        storage._pool_local.loop = None
        try:
            with patch('hippo.storage.AsyncConnectionPool', DummyPool):
                pool = await storage.get_pool('postgresql://example')
        finally:
            storage._pool_local.pool, storage._pool_local.dsn, storage._pool_local.loop = previous

        self.assertIsInstance(pool, DummyPool)
        # The pool is created closed and opened explicitly, so the async
        # warm-up callback cannot run before a loop is running.
        self.assertIn('open', captured)
        self.assertFalse(captured['open'])


if __name__ == '__main__':
    unittest.main()
