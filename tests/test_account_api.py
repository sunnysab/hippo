import unittest
from types import SimpleNamespace

from hippo import server


class AccountApiTest(unittest.TestCase):
    def test_list_accounts_normalizes_null_alias_to_empty_string(self) -> None:
        storage = SimpleNamespace(
            accounts=SimpleNamespace(
                list_accounts_paginated=lambda **_: {
                    'accounts': [{
                        'biz': 'gh_1',
                        'nickname': 'Alpha',
                        'alias': None,
                        'avatar_url': '/api/account/gh_1/avatar',
                    }],
                    'page': 1,
                    'page_size': 20,
                    'total': 1,
                },
            ),
        )

        payload = server.list_accounts(storage=storage)

        self.assertEqual('', payload['accounts'][0]['alias'])


if __name__ == '__main__':
    unittest.main()
