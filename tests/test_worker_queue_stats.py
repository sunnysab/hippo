"""Queue sampling cannot hold up the heartbeat or overwrite a valid sample on failure."""

import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hippo.sync_settings import QUEUE_STATS_KEY, WORKER_HEARTBEAT_KEY
from hippo.sync_worker import publish_worker_state, sample_queue_stats


@asynccontextmanager
async def transaction():
    yield


def storage():
    return SimpleNamespace(
        transaction=transaction,
        conn=SimpleNamespace(execute=AsyncMock()),
        meta=SimpleNamespace(set=AsyncMock()),
        article_queue=SimpleNamespace(stats=AsyncMock(return_value={'pending': 3})),
        images=SimpleNamespace(count_backlog=AsyncMock(return_value={'pending': 4})),
        media_jobs=SimpleNamespace(stats_by_source=AsyncMock(return_value={})),
    )


class WorkerQueueStatsTest(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_does_not_wait_for_sampling(self):
        sample = storage()
        beat = storage()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow():
            started.set()
            await release.wait()
            return {'pending': 4}

        sample.images.count_backlog.side_effect = slow
        task = asyncio.create_task(sample_queue_stats(sample))
        try:
            await asyncio.wait_for(started.wait(), 1)
            with patch('hippo.sync_worker._last_worker_state_at', 0):
                await asyncio.wait_for(publish_worker_state(beat), 1)
            beat.meta.set.assert_awaited_once()
            assert beat.meta.set.await_args.args[0] == WORKER_HEARTBEAT_KEY
            beat.article_queue.stats.assert_not_awaited()
            sample.meta.set.assert_not_awaited()
        finally:
            release.set()
            await task
        assert sample.meta.set.await_args.args[0] == QUEUE_STATS_KEY
        assert 'sampled_at' in sample.meta.set.await_args.args[1]

    async def test_failed_sample_preserves_previous_watermark(self):
        sample = storage()
        sample.images.count_backlog.side_effect = TimeoutError('slow image query')
        with pytest.raises(TimeoutError):
            await sample_queue_stats(sample)
        sample.meta.set.assert_not_awaited()
        sample.conn.execute.assert_awaited_once_with("SET LOCAL statement_timeout = '10s'")
