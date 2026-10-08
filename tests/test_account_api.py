import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.api.routers import account as account_api
from hippo.article_queries import _ensure_image_visible
from hippo.exceptions import ApiError


class _FakeSource:
    """WeixinSource 的替身：只实现 async with + 一次搜索。"""

    def __init__(self, items: list[dict]) -> None:
        self.search_public_accounts = AsyncMock(return_value=items)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


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

    async def test_search_caches_avatars_under_the_gh_id(self) -> None:
        # accounts.biz 是 fakeid，搜索头像接口却按 gh_id 取图，落库必须用 gh_id。
        storage = SimpleNamespace(
            accounts=SimpleNamespace(list_followed_accounts=AsyncMock(return_value=[])),
        )
        items = [
            {
                'userName': 'gh_abc123',
                'nickName': '中投数研',
                'alias': 'gh_abc',
                'headImgUrl': 'http://wx.qlogo.cn/mmhead/avatar/132',
            },
            {
                'userName': 'gh_none456',
                'nickName': '无头像号',
                'alias': '',
                'headImgUrl': '',
            },
        ]
        source = _FakeSource(items)
        user = SimpleNamespace(id=1, username='admin', role='admin')

        with (
            patch('hippo.api.routers.account.WeixinSource', return_value=source),
            patch('hippo.api.routers.account._upsert_avatar_url', AsyncMock()) as upsert,
        ):
            payload = await account_api.search_account(q='中投', storage=storage, user=user)

        upsert.assert_awaited_once_with(storage, 'gh_abc123', 'http://wx.qlogo.cn/mmhead/avatar/132')
        self.assertEqual('/api/account/search/gh_abc123/avatar', payload['results'][0]['avatar_url'])
        # 微信没给头像时留空，让前端走灰色占位块而不是一个必然 404 的代理地址。
        self.assertEqual('', payload['results'][1]['avatar_url'])

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
