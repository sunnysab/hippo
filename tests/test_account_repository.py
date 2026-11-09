import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.repositories.account import AccountRepository

LATEST_PUBLICATION_QUERY = (
    '(SELECT to_timestamp(ar.publish_at) FROM articles ar'
    ' WHERE ar.biz = a.biz AND ar.publish_at IS NOT NULL'
    ' ORDER BY ar.publish_at DESC LIMIT 1) AS latest_publish_at'
)


class AccountPublicationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # This class exercises publication SQL; sync read-model tests live separately.
        patcher = patch('hippo.repositories.account.enrich_account_sync', new_callable=AsyncMock)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_internal_accounts_are_excluded_from_both_page_and_total(self) -> None:
        cursor = AsyncMock()
        cursor.__aenter__.return_value = cursor
        cursor.fetchall.return_value = []
        cursor.fetchone.return_value = {'total': 0}
        repository = AccountRepository(SimpleNamespace(cursor=lambda **kwargs: cursor))

        await repository.list_accounts_paginated(user_id=7)

        for call in cursor.execute.await_args_list:
            self.assertIn("a.biz <> 'adhoc'", call.args[0])

    async def test_internal_accounts_are_excluded_from_followed_accounts_and_source_counts(self) -> None:
        cursor = AsyncMock()
        cursor.__aenter__.return_value = cursor
        cursor.fetchall.return_value = []
        repository = AccountRepository(SimpleNamespace(cursor=lambda **kwargs: cursor))

        await repository.list_followed_accounts(7, group='Default')
        await repository.count_by_source(7)

        for call in cursor.execute.await_args_list:
            self.assertIn("a.biz <> 'adhoc'", call.args[0])

    async def test_the_paginated_list_reads_the_latest_publication_for_each_account(self) -> None:
        published = datetime(2026, 10, 19, 17, tzinfo=UTC)
        rows = [{'biz': 'youtube:UC123', 'latest_publish_at': published}]
        cursor = AsyncMock()
        cursor.__aenter__.return_value = cursor
        cursor.fetchall.return_value = rows
        cursor.fetchone.return_value = {'total': 1}
        repository = AccountRepository(SimpleNamespace(cursor=lambda **kwargs: cursor))

        result = await repository.list_accounts_paginated(user_id=7, group_ids=[1], source='youtube')

        query, params = cursor.execute.await_args_list[0].args
        self.assertIn(LATEST_PUBLICATION_QUERY, ' '.join(query.split()))
        self.assertEqual(2, query.count('ORDER BY a.nickname ASC, a.biz ASC'))
        self.assertEqual([7, [1], 'youtube', 20, 0], params)
        self.assertLess(query.index('LIMIT %s OFFSET %s'), query.index(LATEST_PUBLICATION_QUERY[:20]))
        self.assertEqual(published, result['accounts'][0]['latest_publish_at'])

    async def test_the_detail_keeps_an_unknown_publication_separate_from_the_sync(self) -> None:
        synced = datetime(2026, 10, 19, 22, tzinfo=UTC)
        cursor = AsyncMock()
        cursor.__aenter__.return_value = cursor
        cursor.fetchone.return_value = {
            'biz': 'youtube:UC123',
            'latest_publish_at': None,
            'last_synced_at': synced,
        }
        repository = AccountRepository(SimpleNamespace(cursor=lambda **kwargs: cursor))

        result = await repository.get_account_detail('youtube:UC123', user_id=7)

        query, params = cursor.execute.await_args.args
        self.assertIn(LATEST_PUBLICATION_QUERY, ' '.join(query.split()))
        self.assertEqual((7, 'youtube:UC123'), params)
        self.assertIsNone(result['latest_publish_at'])
        self.assertEqual(synced, result['last_synced_at'])
