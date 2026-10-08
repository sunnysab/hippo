"""历史回填：游标推进、到底判定、账号级停用约束。"""

from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.sync_worker import backfill_account_history, maybe_enqueue_backfill_job


class _FakeSource:
    async def __aenter__(self) -> _FakeSource:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


def _storage(account: SimpleNamespace, *, syncable: bool = True) -> SimpleNamespace:
    @asynccontextmanager
    async def transaction():
        yield None

    return SimpleNamespace(
        transaction=transaction,
        accounts=SimpleNamespace(
            get_account=AsyncMock(return_value=account),
            list_syncable_accounts=AsyncMock(return_value=[account] if syncable else []),
            set_backfill_cursor=AsyncMock(),
            mark_backfill_done=AsyncMock(),
        ),
    )


class BackfillTest(unittest.IsolatedAsyncioTestCase):
    async def test_requeues_a_pending_backfill(self) -> None:
        @asynccontextmanager
        async def transaction():
            yield None

        storage = SimpleNamespace(
            transaction=transaction,
            accounts=SimpleNamespace(
                list_pending_backfill=AsyncMock(return_value=[SimpleNamespace(biz='Mz9')]),
            ),
            sync_jobs=SimpleNamespace(
                has_active_job=AsyncMock(return_value=False),
                create_job=AsyncMock(),
            ),
        )

        created = await maybe_enqueue_backfill_job(storage)

        self.assertTrue(created)
        storage.sync_jobs.create_job.assert_awaited_once_with(
            trigger_type='backfill',
            biz_list=['Mz9'],
        )

    async def test_does_not_requeue_while_a_job_is_active(self) -> None:
        storage = SimpleNamespace(
            sync_jobs=SimpleNamespace(has_active_job=AsyncMock(return_value=True)),
        )

        self.assertFalse(await maybe_enqueue_backfill_job(storage))

    async def test_nothing_pending_queues_nothing(self) -> None:
        storage = SimpleNamespace(
            accounts=SimpleNamespace(list_pending_backfill=AsyncMock(return_value=[])),
            sync_jobs=SimpleNamespace(
                has_active_job=AsyncMock(return_value=False),
                create_job=AsyncMock(),
            ),
        )

        self.assertFalse(await maybe_enqueue_backfill_job(storage))
        storage.sync_jobs.create_job.assert_not_awaited()

    async def test_follows_the_cursor_until_the_last_page(self) -> None:
        pages = [
            SimpleNamespace(listed=20, enqueued=20, next_offset='cur-1', is_end=False),
            SimpleNamespace(listed=15, enqueued=10, next_offset=None, is_end=True),
        ]
        calls: list[dict] = []

        async def sync_account(**kwargs):
            calls.append(kwargs)
            return pages[len(calls) - 1]

        sync_impl = SimpleNamespace(sync_account=sync_account)
        account = SimpleNamespace(biz='Mz1', nickname='A', gh_id='gh_1', alias=None, backfill_cursor=None)
        storage = _storage(account)

        with (
            patch('hippo.sync_worker.WeixinSource', return_value=_FakeSource()),
            patch('hippo.sync_worker.WeixinArticleSync', return_value=sync_impl),
            patch('hippo.sync_worker.asyncio.sleep', AsyncMock()),
        ):
            result = await backfill_account_history(storage, biz='Mz1')

        # 第一页从最新开始（空游标），第二页带上第一页给出的游标
        self.assertEqual(['', 'cur-1'], [call['offset'] for call in calls])
        self.assertEqual(30, result.report.total_saved)
        storage.accounts.set_backfill_cursor.assert_awaited_once_with('Mz1', 'cur-1')
        storage.accounts.mark_backfill_done.assert_awaited_once_with('Mz1')

    async def test_resumes_from_a_stored_cursor(self) -> None:
        calls: list[dict] = []

        async def sync_account(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(listed=5, enqueued=0, next_offset=None, is_end=True)

        sync_impl = SimpleNamespace(sync_account=sync_account)
        account = SimpleNamespace(biz='Mz2', nickname='B', gh_id='gh_2', alias=None, backfill_cursor='cur-9')
        storage = _storage(account)

        with (
            patch('hippo.sync_worker.WeixinSource', return_value=_FakeSource()),
            patch('hippo.sync_worker.WeixinArticleSync', return_value=sync_impl),
        ):
            await backfill_account_history(storage, biz='Mz2')

        self.assertEqual(['cur-9'], [call['offset'] for call in calls])

    async def test_skips_when_every_subscriber_is_disabled(self) -> None:
        account = SimpleNamespace(biz='Mz3', nickname='C', gh_id='gh_3', alias=None, backfill_cursor=None)
        storage = _storage(account, syncable=False)

        result = await backfill_account_history(storage, biz='Mz3')

        self.assertEqual('skipped', result.status['status'])
        storage.accounts.mark_backfill_done.assert_awaited_once_with('Mz3')


if __name__ == '__main__':
    unittest.main()
