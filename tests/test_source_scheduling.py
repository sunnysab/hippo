"""Source polling checks elapsed time without requesting WeChat lists."""

import contextlib
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from hippo.api.routers.account import _update_account, batch_update_accounts
from hippo.api.schemas.account import AccountBatchRequest
from hippo.exceptions import ApiError
from hippo.repositories.account import AccountRepository
from hippo.sync_service import _next_caixin_history_account
from hippo.sync_settings import POLLING_SOURCES, default_sync_settings
from hippo.sync_worker import maybe_enqueue_scheduled_job


def _account(biz, source, hours_ago=None, interval=None):
    return SimpleNamespace(
        biz=biz,
        source=source,
        last_synced_at=datetime.now(UTC) - timedelta(hours=hours_ago) if hours_ago is not None else None,
        sync_interval_days=interval,
    )


class SourceSchedulingTest(unittest.IsolatedAsyncioTestCase):
    def _storage(self, accounts, active=()):
        meta = {}

        async def set_meta(key, value):
            meta[key] = value

        return SimpleNamespace(
            meta=SimpleNamespace(get=AsyncMock(side_effect=meta.get), set=AsyncMock(side_effect=set_meta)),
            accounts=SimpleNamespace(
                list_syncable_accounts=AsyncMock(
                    side_effect=lambda *, source, **kwargs: [a for a in accounts if a.source == source]
                )
            ),
            sync_jobs=SimpleNamespace(
                has_active_job=AsyncMock(side_effect=lambda *, source, **kwargs: source in active),
                create_job=AsyncMock(),
            ),
            transaction=contextlib.nullcontext,
        )

    async def test_only_due_accounts_are_queued_and_wechat_is_never_listed(self):
        storage = self._storage(
            [
                _account('wx', 'weixin'),
                _account('recent', 'feed', 23),
                _account('due', 'feed', 24),
                _account('new', 'youtube'),
                _account('weekly', 'podcast', 48, 7),
            ]
        )
        with patch('hippo.sync_worker.get_sync_settings', AsyncMock(return_value=default_sync_settings())):
            self.assertTrue(await maybe_enqueue_scheduled_job(storage))

        calls = storage.accounts.list_syncable_accounts.await_args_list
        self.assertEqual(set(POLLING_SOURCES), {call.kwargs['source'] for call in calls})
        jobs = {call.kwargs['source']: call.kwargs['biz_list'] for call in storage.sync_jobs.create_job.await_args_list}
        self.assertEqual({'feed': ['due'], 'youtube': ['new']}, jobs)

    async def test_source_defaults_and_subscription_overrides_both_apply(self):
        storage = self._storage(
            [
                _account('default', 'youtube', 48),
                _account('daily', 'youtube', 24, 1),
                _account('fresh', 'feed', 24),
            ]
        )
        settings = default_sync_settings()
        settings['source_intervals_days']['youtube'] = 3
        with patch('hippo.sync_worker.get_sync_settings', AsyncMock(return_value=settings)):
            await maybe_enqueue_scheduled_job(storage)
        jobs = {call.kwargs['source']: call.kwargs['biz_list'] for call in storage.sync_jobs.create_job.await_args_list}
        self.assertEqual({'youtube': ['daily'], 'feed': ['fresh']}, jobs)

    async def test_a_due_caixin_channel_gets_a_scheduled_job(self):
        storage = self._storage([_account('caixin:132', 'caixin', 24)])
        with patch('hippo.sync_worker.get_sync_settings', AsyncMock(return_value=default_sync_settings())):
            self.assertTrue(await maybe_enqueue_scheduled_job(storage))
        storage.sync_jobs.create_job.assert_awaited_once_with(
            trigger_type='scheduled', source='caixin', biz_list=['caixin:132']
        )

    async def test_caixin_waits_for_the_instance_hour_interval_and_ignores_account_overrides(self):
        storage = self._storage([
            _account('caixin:132', 'caixin', 5, 1),
            _account('caixin:124', 'caixin', 7, 30),
        ])
        with patch('hippo.sync_worker.get_sync_settings', AsyncMock(return_value=default_sync_settings())):
            await maybe_enqueue_scheduled_job(storage)
        storage.sync_jobs.create_job.assert_awaited_once_with(
            trigger_type='scheduled', source='caixin', biz_list=['caixin:124']
        )

    async def test_one_busy_source_does_not_block_another(self):
        storage = self._storage([_account('feed', 'feed'), _account('youtube', 'youtube')], active=('feed',))
        with patch('hippo.sync_worker.get_sync_settings', AsyncMock(return_value=default_sync_settings())):
            await maybe_enqueue_scheduled_job(storage)
        storage.sync_jobs.create_job.assert_awaited_once_with(
            trigger_type='scheduled', source='youtube', biz_list=['youtube']
        )

    async def test_a_failed_attempt_cannot_be_enqueued_every_worker_tick(self):
        storage = self._storage([_account('feed', 'feed')])
        with patch('hippo.sync_worker.get_sync_settings', AsyncMock(return_value=default_sync_settings())):
            self.assertTrue(await maybe_enqueue_scheduled_job(storage))
            self.assertFalse(await maybe_enqueue_scheduled_job(storage))
        self.assertEqual(1, storage.sync_jobs.create_job.await_count)


class SharedAccountIntervalTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_subscriber_following_the_default_is_not_ignored_by_min(self):
        for source_default, override, expected in [(1, 7, 1), (7, 1, 1), (3, None, 3)]:
            with self.subTest(source_default=source_default, override=override):
                cursor = AsyncMock()
                cursor.__aenter__.return_value = cursor
                cursor.fetchall.return_value = [
                    {
                        'biz': 'feed:shared',
                        'nickname': 'Shared',
                        'source': 'feed',
                        'sync_interval_days': override,
                        'uses_source_interval': True,
                    }
                ]
                repo = AccountRepository(SimpleNamespace(cursor=Mock(return_value=cursor)))
                accounts = await repo.list_syncable_accounts(source_intervals_days={'feed': source_default})
                self.assertEqual(expected, accounts[0].sync_interval_days)


class CaixinIntervalApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_single_account_interval_cannot_be_set_for_caixin(self):
        storage = SimpleNamespace(
            accounts=SimpleNamespace(
                get_account=AsyncMock(return_value=SimpleNamespace(source='caixin')),
            ),
        )
        with self.assertRaisesRegex(ApiError, 'source-wide') as raised:
            await _update_account(storage, 1, 'caixin:132', {'sync_interval_days': 1})
        self.assertEqual('caixin.interval_global', raised.exception.code)

    async def test_batch_interval_cannot_touch_caixin(self):
        storage = SimpleNamespace(
            accounts=SimpleNamespace(
                get_account=AsyncMock(return_value=SimpleNamespace(source='caixin')),
            ),
        )
        with self.assertRaisesRegex(ApiError, 'source-wide') as raised:
            await batch_update_accounts(
                AccountBatchRequest(biz_list=['caixin:132'], sync_interval_days=1),
                storage,
                SimpleNamespace(id=1),
            )
        self.assertEqual('caixin.interval_global', raised.exception.code)


class CaixinHistoryRotationTest(unittest.TestCase):
    def test_history_pages_rotate_across_enabled_channels(self):
        accounts = [
            SimpleNamespace(biz='caixin:124'),
            SimpleNamespace(biz='caixin:125'),
            SimpleNamespace(biz='caixin:132'),
        ]

        self.assertEqual('caixin:124', _next_caixin_history_account(accounts, None).biz)
        self.assertEqual('caixin:132', _next_caixin_history_account(accounts, 'caixin:125').biz)
        self.assertEqual('caixin:124', _next_caixin_history_account(accounts, 'caixin:132').biz)
