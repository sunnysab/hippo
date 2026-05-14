from __future__ import annotations

import unittest
from unittest.mock import patch

from hippo import storage


class StoragePoolTest(unittest.TestCase):
    def test_get_pool_passes_explicit_open_flag(self) -> None:
        captured: dict[str, object] = {}

        class DummyPool:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)

            def close(self) -> None:
                return None

        previous_pool = storage._PG_POOL
        previous_dsn = storage._PG_POOL_DSN
        storage._PG_POOL = None
        storage._PG_POOL_DSN = None
        try:
            with patch('hippo.storage.ConnectionPool', DummyPool):
                pool = storage._get_pool('postgresql://example')
        finally:
            storage._PG_POOL = previous_pool
            storage._PG_POOL_DSN = previous_dsn

        self.assertIsInstance(pool, DummyPool)
        self.assertIn('open', captured)
        self.assertTrue(captured['open'])


if __name__ == '__main__':
    unittest.main()
