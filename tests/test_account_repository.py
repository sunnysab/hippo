import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hippo.repositories.account import AccountRepository

LATEST_PUBLICATION_QUERY = (
    '(SELECT to_timestamp(ar.publish_at) FROM articles ar'
    ' WHERE ar.biz = a.biz AND ar.publish_at IS NOT NULL'
    ' ORDER BY ar.publish_at DESC LIMIT 1) AS latest_publish_at'
)


class AccountPublicationTest(unittest.IsolatedAsyncioTestCase):
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
