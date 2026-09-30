from __future__ import annotations

import unittest
from unittest.mock import patch

from hippo import storage


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
