"""Sources advance independently without sharing cancellation or database handles."""

import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo import sync_service
from hippo.sync_core import _get_cancel_event, request_sync_cancel, reset_sync_cancel
from hippo.sync_types import SyncReport


class ParallelSyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_source_job_uses_independent_connections_and_advances_both_sources(self):
        accounts = [SimpleNamespace(biz='wx', source='weixin'), SimpleNamespace(biz='yt', source='youtube')]
        entered = set()
        ready = asyncio.Event()
        handles = []
        pool = asyncio.Semaphore(2)
        fail_source = None

        @asynccontextmanager
        async def storage():
            async with pool:
                handle = SimpleNamespace(
                    accounts=SimpleNamespace(list_syncable_accounts=AsyncMock(return_value=accounts)),
                    rollback=AsyncMock(),
                )
                handles.append(handle)
                yield handle

        @asynccontextmanager
        async def container(**kwargs):
            yield SimpleNamespace(adapters={}, downloader=None, weixin_unavailable=None)

        async def sync_accounts(instance, *, accounts, **kwargs):
            entered.add(accounts[0].source)
            if len(entered) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 1)
            if accounts[0].source == fail_source:
                raise RuntimeError('source unavailable')
            return SyncReport(total_saved=1, summary=[], details=[], downloaded=0, accounts_done=1)

        with (
            patch.object(sync_service, 'open_storage', storage),
            patch.object(sync_service, 'build_sync_container', container),
            patch.object(sync_service, 'get_sync_settings', AsyncMock(return_value={})),
            patch.object(sync_service, 'get_media_settings', AsyncMock(return_value={})),
            patch.object(sync_service, 'set_sync_state', AsyncMock()),
            patch.object(sync_service, '_persist_sync_outcome', AsyncMock(return_value={'status': 'success'})),
            patch.object(sync_service.ArticleSyncService, 'sync_accounts', sync_accounts),
        ):
            result = await sync_service.run_sync_job()
            fail_source = 'weixin'
            partial = await sync_service.run_sync_job()
            self.assertEqual('source unavailable', partial.error)
            self.assertEqual(1, partial.report.total_saved)
            self.assertEqual(1, partial.report.accounts_done)
            empty = await sync_service.run_sync_job(scope_sources=())
            self.assertIsNone(empty.error)
            self.assertEqual(0, empty.report.total_saved)
        self.assertIsNone(result.error)
        self.assertEqual(2, result.report.total_saved)
        self.assertEqual(2, result.report.accounts_done)
        self.assertEqual(10, len({id(handle) for handle in handles}))

    async def test_cancelling_one_job_does_not_cancel_another(self):
        ready = asyncio.Event()
        cancelled = asyncio.Event()

        async def second():
            reset_sync_cancel()
            await ready.wait()
            request_sync_cancel()
            cancelled.set()
            return _get_cancel_event().is_set()

        # The first job stays clear; only the second job's own context is set.
        async def independent():
            reset_sync_cancel()
            ready.set()
            await cancelled.wait()
            return _get_cancel_event().is_set()

        clear, stopped = await asyncio.gather(independent(), second())
        self.assertFalse(clear)
        self.assertTrue(stopped)

    async def test_worker_rejects_an_undersized_pool_before_starting_consumers(self):
        from hippo import sync_worker

        with (
            patch.object(
                sync_worker, 'get_settings', return_value=SimpleNamespace(database=SimpleNamespace(pool_max=5))
            ),
            patch.object(sync_worker, '_body_drain_loop', AsyncMock()) as drain,
        ):
            with self.assertRaisesRegex(ValueError, 'pool_max >= 6'):
                await sync_worker.run_sync_worker(configure_process=False)
            drain.assert_not_called()
