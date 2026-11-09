"""A subscription may rely on pushes only after the bot confirms the follow."""

import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.api.routers import account as account_api
from hippo.exceptions import ApiError
from hippo.weixin_daemon import LIST_TIMEOUT_SECONDS, WeixinSource, WeixinUnavailableError


class FollowAccountTest(unittest.IsolatedAsyncioTestCase):
    async def test_follow_requires_confirmation_and_uses_a_bounded_rpc(self):
        source = object.__new__(WeixinSource)
        source._bot = SimpleNamespace(call=AsyncMock(return_value={'followed': True}))

        await source.follow_account(' gh_publisher ')

        source._bot.call.assert_awaited_once_with('follow_biz', {'gh_id': 'gh_publisher'}, timeout=LIST_TIMEOUT_SECONDS)

    async def test_an_unconfirmed_follow_is_an_error(self):
        for response in (None, [], {}, {'followed': False}, {'followed': 'yes'}):
            with self.subTest(response=response):
                source = object.__new__(WeixinSource)
                source._bot = SimpleNamespace(call=AsyncMock(return_value=response))

                with self.assertRaisesRegex(WeixinUnavailableError, 'follow was not confirmed'):
                    await source.follow_account('gh_publisher')

    async def test_a_missing_gh_id_never_calls_the_daemon(self):
        source = object.__new__(WeixinSource)
        source._bot = SimpleNamespace(call=AsyncMock())

        with self.assertRaisesRegex(ValueError, 'gh_id is required'):
            await source.follow_account(' ')

        source._bot.call.assert_not_awaited()


class SubscribeFollowTest(unittest.IsolatedAsyncioTestCase):
    def _storage(self):
        account = SimpleNamespace(
            biz='MzPublisher',
            nickname='Publisher',
            alias='publisher',
            round_head_img=None,
            group_id=1,
            backfill_state='pending',
        )
        return SimpleNamespace(
            transaction=contextlib.nullcontext,
            rollback=AsyncMock(),
            accounts=SimpleNamespace(
                upsert_account=AsyncMock(return_value=account),
                get_account=AsyncMock(side_effect=LookupError('Unknown account')),
            ),
            subscriptions=SimpleNamespace(upsert=AsyncMock()),
            sync_jobs=SimpleNamespace(create_job=AsyncMock()),
        )

    def _source(self):
        source = AsyncMock()
        source.__aenter__.return_value = source
        return source

    async def test_follow_is_confirmed_before_saving_the_subscription(self):
        storage = self._storage()
        source = self._source()

        async def follow(gh_id):
            storage.accounts.upsert_account.assert_not_awaited()
            storage.subscriptions.upsert.assert_not_awaited()
            storage.sync_jobs.create_job.assert_not_awaited()

        source.follow_account.side_effect = follow
        with patch.object(account_api, 'WeixinSource', return_value=source):
            await account_api.subscribe_weixin_account(
                storage, 1, biz='MzPublisher', nickname='Publisher', gh_id='gh_publisher', group_id=1
            )

        source.follow_account.assert_awaited_once_with('gh_publisher')
        storage.subscriptions.upsert.assert_awaited_once_with(1, 'MzPublisher', group_id=1)
        storage.sync_jobs.create_job.assert_awaited_once_with(
            trigger_type='backfill', biz_list=['MzPublisher'], source='weixin'
        )

    async def test_a_failed_follow_does_not_save_a_subscription_or_history_job(self):
        storage = self._storage()
        source = self._source()
        source.follow_account.side_effect = RuntimeError('daemon refused the follow')

        with patch.object(account_api, 'WeixinSource', return_value=source), self.assertRaises(ApiError) as error:
            await account_api.subscribe_weixin_account(
                storage, 1, biz='MzPublisher', nickname='Publisher', gh_id='gh_publisher', group_id=1
            )

        self.assertEqual(502, error.exception.status)
        self.assertEqual('daemon.follow_failed', error.exception.code)
        self.assertNotIn('daemon refused', str(error.exception))
        storage.accounts.upsert_account.assert_not_awaited()
        storage.subscriptions.upsert.assert_not_awaited()
        storage.sync_jobs.create_job.assert_not_awaited()

    async def test_an_existing_account_supplies_the_push_identity(self):
        storage = self._storage()
        storage.accounts.get_account.side_effect = None
        storage.accounts.get_account.return_value = SimpleNamespace(gh_id='gh_existing', alias='existing')
        source = self._source()

        async def follow(gh_id):
            storage.rollback.assert_awaited_once()

        source.follow_account.side_effect = follow
        with patch.object(account_api, 'WeixinSource', return_value=source):
            await account_api.subscribe_weixin_account(storage, 1, biz='MzPublisher', nickname='Publisher', group_id=1)

        storage.accounts.get_account.assert_awaited_once_with(biz='MzPublisher', fallback_to_default=False)
        source.follow_account.assert_awaited_once_with('gh_existing')

    async def test_an_alias_resolves_the_push_identity_before_following(self):
        storage = self._storage()
        source = self._source()
        source.list_articles.return_value = SimpleNamespace(gh_id='gh_resolved')

        with patch.object(account_api, 'WeixinSource', return_value=source):
            await account_api.subscribe_weixin_account(
                storage, 1, biz='MzPublisher', nickname='Publisher', alias='publisher', group_id=1
            )

        source.list_articles.assert_awaited_once_with(source_key='publisher', biz='MzPublisher', pages=1)
        source.follow_account.assert_awaited_once_with('gh_resolved')
        saved = storage.accounts.upsert_account.await_args.args[0]
        self.assertEqual('gh_resolved', saved.gh_id)

    async def test_an_account_without_a_push_identity_is_rejected(self):
        storage = self._storage()

        with patch.object(account_api, 'WeixinSource') as source, self.assertRaises(ApiError) as error:
            await account_api.subscribe_weixin_account(storage, 1, biz='MzPublisher', nickname='Publisher', group_id=1)

        self.assertEqual(422, error.exception.status)
        self.assertEqual('account.missing_gh_id', error.exception.code)
        source.assert_not_called()
        storage.accounts.upsert_account.assert_not_awaited()
        storage.subscriptions.upsert.assert_not_awaited()
