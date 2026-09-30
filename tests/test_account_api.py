import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hippo.api.routers import account as account_api


class AccountApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_list_accounts_normalizes_null_alias_to_empty_string(self) -> None:
        storage = SimpleNamespace(
            accounts=SimpleNamespace(
                list_accounts_paginated=AsyncMock(return_value={
                    'accounts': [{
                        'biz': 'gh_1',
                        'nickname': 'Alpha',
                        'alias': None,
                        'avatar_url': '/api/account/gh_1/avatar',
                    }],
                    'page': 1,
                    'page_size': 20,
                    'total': 1,
                }),
            ),
        )

        user = SimpleNamespace(id=1, username='admin', role='admin')
        payload = await account_api.list_accounts(storage=storage, user=user)

        self.assertEqual('', payload['accounts'][0]['alias'])


if __name__ == '__main__':
    unittest.main()
