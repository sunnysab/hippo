"""History resume is scoped to a live subscription and never discards its cursor."""

import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hippo.api.routers.account import resume_account_backfill
from hippo.exceptions import ApiError


class HistoryResumeTest(unittest.IsolatedAsyncioTestCase):
    def storage(self, *, history='paused', source='weixin', disabled=False):
        cursor = AsyncMock()
        cursor.__aenter__.return_value = cursor
        cursor.fetchone.return_value = (history,)
        return SimpleNamespace(
            conn=SimpleNamespace(cursor=lambda **_: cursor),
            transaction=contextlib.nullcontext,
            accounts=SimpleNamespace(
                get_account_detail=AsyncMock(return_value={'source': source, 'is_disabled': disabled}),
                set_backfill_state=AsyncMock(),
            ),
            sync_jobs=SimpleNamespace(
                get_active_backfill=AsyncMock(return_value=None),
                create_job=AsyncMock(return_value=SimpleNamespace(status='queued', task_id='job')),
            ),
        )

    async def test_resumes_under_transaction_without_resetting_cursor(self):
        storage = self.storage()
        result = await resume_account_backfill('one', storage=storage, user=SimpleNamespace(id=7))
        self.assertEqual({'status': 'queued', 'task_id': 'job'}, result)
        storage.accounts.get_account_detail.assert_awaited_once_with('one', user_id=7)
        storage.accounts.set_backfill_state.assert_awaited_once_with('one', 'pending')
        storage.sync_jobs.create_job.assert_awaited_once_with(
            trigger_type='backfill', biz_list=['one'], source='weixin'
        )

    async def test_resume_waits_for_running_cancellation_to_finish(self):
        storage = self.storage()
        storage.sync_jobs.get_active_backfill.return_value = SimpleNamespace(status='cancelling', task_id='old')
        with self.assertRaises(ApiError) as error:
            await resume_account_backfill('one', storage=storage, user=SimpleNamespace(id=7))
        self.assertEqual('history.cancelling', error.exception.code)
        storage.accounts.set_backfill_state.assert_not_awaited()
        storage.sync_jobs.create_job.assert_not_awaited()

    async def test_completed_history_is_never_restarted(self):
        storage = self.storage(history='done')
        result = await resume_account_backfill('one', storage=storage, user=SimpleNamespace(id=7))
        self.assertEqual('complete', result['status'])
        storage.accounts.set_backfill_state.assert_not_awaited()
        storage.sync_jobs.create_job.assert_not_awaited()

    async def test_foreign_disabled_and_unsupported_subscriptions_cannot_resume(self):
        for source, disabled, expected in [('youtube', False, 400), ('weixin', True, 409)]:
            storage = self.storage(source=source, disabled=disabled)
            with self.assertRaises(ApiError) as error:
                await resume_account_backfill('one', storage=storage, user=SimpleNamespace(id=7))
            self.assertEqual(expected, error.exception.status)
            storage.sync_jobs.create_job.assert_not_awaited()
        storage = self.storage()
        storage.accounts.get_account_detail.side_effect = LookupError()
        with self.assertRaises(ApiError) as error:
            await resume_account_backfill('foreign', storage=storage, user=SimpleNamespace(id=7))
        self.assertEqual(404, error.exception.status)
