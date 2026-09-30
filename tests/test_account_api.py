import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.api.routers import account as account_api
from hippo.article_queries import _ensure_image_visible
from hippo.exceptions import ApiError


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


    async def test_an_image_from_an_unsubscribed_account_is_a_404(self) -> None:
        # No subscription row matches, so the lookup comes back empty.
        storage = SimpleNamespace(
            meta=SimpleNamespace(get=AsyncMock(return_value=None)),
        )
        with (
            patch('hippo.article_queries.fetchone_row', AsyncMock(return_value=None)),
            self.assertRaises(ApiError) as ctx,
        ):
            await _ensure_image_visible(storage, 7, 123)
        self.assertEqual(404, ctx.exception.status)

    async def test_an_own_image_passes_the_check(self) -> None:
        storage = SimpleNamespace(meta=SimpleNamespace(get=AsyncMock(return_value=None)))
        with patch('hippo.article_queries.fetchone_row', AsyncMock(return_value={'?column?': 1})):
            await _ensure_image_visible(storage, 7, 123)


if __name__ == '__main__':
    unittest.main()
