"""Avatar caching: which exit it leaves through, and that it does not block.

An avatar is fetched while a request is open, so it must go through the shared
async client rather than a blocking one-off call, and it must use the exit its own
account's source uses: a YouTube account's avatar sits on a host the box cannot
reach directly, while fetching a WeChat avatar through the operator's proxy only
adds a hop. The URL itself is untrusted, so the fetch is delegated to the public
image guard (`hippo.public_image`).
"""

import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from hippo import avatar as avatar_module
from hippo.avatar import _avatar_proxy, _fetch_and_cache_avatar
from hippo.media_settings import default_media_settings

SETTINGS = {
    **default_media_settings(),
    'proxy_default': 'http://default:1',
    'proxy_youtube': 'http://youtube:2',
    'proxy_weixin': 'direct',
}


class AvatarProxyTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_youtube_account_leaves_through_the_youtube_exit(self) -> None:
        with (
            patch.object(avatar_module, 'fetchone_row', AsyncMock(return_value={'source': 'youtube'})),
            patch('hippo.media_settings.get_media_settings', AsyncMock(return_value=SETTINGS)),
        ):
            self.assertEqual('http://youtube:2', await _avatar_proxy(SimpleNamespace(), 'youtube:UC123'))

    async def test_a_wechat_account_stays_direct(self) -> None:
        with (
            patch.object(avatar_module, 'fetchone_row', AsyncMock(return_value={'source': 'weixin'})),
            patch('hippo.media_settings.get_media_settings', AsyncMock(return_value=SETTINGS)),
        ):
            self.assertEqual('', await _avatar_proxy(SimpleNamespace(), 'Mz123'))

    async def test_a_biz_that_is_not_an_account_row_stays_on_wechat(self) -> None:
        # The search path caches an avatar under gh_id, which no accounts row knows.
        with (
            patch.object(avatar_module, 'fetchone_row', AsyncMock(return_value=None)),
            patch('hippo.media_settings.get_media_settings', AsyncMock(return_value=SETTINGS)),
        ):
            self.assertEqual('', await _avatar_proxy(SimpleNamespace(), 'gh_abc'))


class AvatarFetchTest(unittest.IsolatedAsyncioTestCase):
    class _Client:
        """Stands in for MPClient: records the exit and exposes the raw client."""

        def __init__(self, seen: dict) -> None:
            self._seen = seen
            self.client = object()

        def __call__(self, *, timeout: float | None = None, proxy: str | None = None):
            self._seen['proxy'] = proxy
            self._seen['timeout'] = timeout
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    def _fake_fetch(self, seen: dict, *, response=None, error: Exception | None = None):
        """Stand in for the public image guard, yielding to the loop when asked."""

        async def fetch(client: object, url: str, *, headers: dict | None = None):
            seen['client'] = client
            seen['url'] = url
            seen['headers'] = headers
            if seen.get('yield'):
                await asyncio.sleep(0.05)
            seen['order'].append('fetch')
            if error is not None:
                raise error
            return response

        return fetch

    def _patch_all(
        self,
        stack: ExitStack,
        seen: dict,
        *,
        source: str = 'youtube',
        response=None,
        error: Exception | None = None,
    ):
        """Patch the proxy lookup, the settings, the client and the guard; returns the store."""
        stack.enter_context(patch.object(avatar_module, 'fetchone_row', AsyncMock(return_value={'source': source})))
        stack.enter_context(patch('hippo.media_settings.get_media_settings', AsyncMock(return_value=SETTINGS)))
        stack.enter_context(patch.object(avatar_module, 'MPClient', self._Client(seen)))
        stack.enter_context(
            patch.object(avatar_module, 'fetch_public_image', self._fake_fetch(seen, response=response, error=error))
        )
        return stack.enter_context(patch.object(avatar_module, '_store_avatar', AsyncMock()))

    async def test_the_avatar_is_fetched_through_the_account_sources_exit(self) -> None:
        seen = {'order': [], 'response': (b'avatar-bytes', 'image/png')}

        with ExitStack() as stack:
            store = self._patch_all(stack, seen, response=(b'avatar-bytes', 'image/png'))
            result = await _fetch_and_cache_avatar(SimpleNamespace(), 'youtube:UC123', 'https://yt3/x=s88.jpg')

        self.assertEqual((b'avatar-bytes', 'image/png'), result)
        self.assertEqual('http://youtube:2', seen['proxy'])
        self.assertEqual(10.0, seen['timeout'])
        self.assertEqual('https://yt3/x=s88.jpg', seen['url'])
        self.assertIn('User-Agent', seen['headers'])
        store.assert_awaited_once()
        self.assertEqual('https://yt3/x=s88.jpg', store.await_args.kwargs['avatar_url'])
        self.assertEqual('image/png', store.await_args.kwargs['content_type'])

    async def test_the_event_loop_keeps_running_while_the_avatar_is_in_flight(self) -> None:
        seen = {'order': [], 'yield': True}
        ran: list[str] = []

        async def other() -> None:
            await asyncio.sleep(0.01)
            ran.append('other')

        with ExitStack() as stack:
            self._patch_all(stack, seen, response=(b'avatar-bytes', 'image/png'))
            task = asyncio.create_task(other())
            await _fetch_and_cache_avatar(SimpleNamespace(), 'youtube:UC123', 'https://yt3/x.jpg')
            await task

        # The fetch yielded instead of doing blocking I/O: the other task ran during it.
        self.assertEqual(['other'], ran)

    async def test_a_refused_fetch_is_not_an_error(self) -> None:
        seen = {'order': [], 'response': None}

        with ExitStack() as stack:
            store = self._patch_all(stack, seen, response=None)
            result = await _fetch_and_cache_avatar(SimpleNamespace(), 'youtube:UC123', 'https://yt3/missing.jpg')

        self.assertIsNone(result)
        store.assert_not_awaited()

    async def test_a_failed_fetch_is_logged_and_returns_none(self) -> None:
        seen = {'order': [], 'response': None}

        with ExitStack() as stack:
            self._patch_all(stack, seen, error=httpx.ConnectError('no route to host'))
            warning = stack.enter_context(patch.object(avatar_module.logger, 'warning'))
            result = await _fetch_and_cache_avatar(SimpleNamespace(), 'youtube:UC123', 'https://yt3/x.jpg')

        self.assertIsNone(result)
        warning.assert_called_once()


if __name__ == '__main__':
    unittest.main()
