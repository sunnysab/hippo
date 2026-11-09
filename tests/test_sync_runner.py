"""Embedded mode must consume the persistent queue instead of bypassing it."""

import asyncio
import unittest
from unittest.mock import patch

from hippo.sync_runner import SyncWorkerRunner


class SyncRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_embedded_runner_uses_same_worker_without_reconfiguring_web_logging(self):
        calls = []
        stopped = asyncio.Event()

        async def worker(**kwargs):
            calls.append(kwargs)
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        runner = SyncWorkerRunner()
        with patch('hippo.sync_worker.run_sync_worker', worker):
            await runner.start()
            await asyncio.sleep(0)
            await runner.start()
            self.assertEqual([{'configure_process': False}], calls)
            await runner.stop()
        self.assertTrue(stopped.is_set())
