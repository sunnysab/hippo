"""Avatar caching: which exit it leaves through, and that it does not block.

An avatar is fetched while a request is open, so it must go through the shared
async client rather than a blocking one-off call, and it must use the exit its own
account's source uses: a YouTube account's avatar sits on a host the box cannot
reach directly, while fetching a WeChat avatar through the operator's proxy only
adds a hop.
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


def _response(*, status: int = 200, content_type: str = 'image/png', body: bytes = b'avatar-bytes') -> httpx.Response:
    return httpx.Response(status, headers={'Content-Type': content_type}, content=body)


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
        """Stands in for MPClient: records the exit and yields the loop."""

        def __init__(self, seen: dict) -> None:
            self._seen = seen

        def __call__(self, *, timeout: float | None = None, proxy: str | None = None):
            self._seen['proxy'] = proxy
            self._seen['timeout'] = timeout
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

        async def get(self, url: str, *, headers: dict | None = None) -> httpx.Response:
            self._seen['url'] = url
            self._seen['headers'] = headers
            await asyncio.sleep(0.05)
            self._seen['order'].append('fetch')
            return self._seen['response']

    def _patch_all(self, stack: ExitStack, seen: dict, *, source: str = 'youtube', client: type | None = None):
        """Patch the proxy lookup, the settings and the HTTP client; returns the store."""
        stack.enter_context(patch.object(avatar_module, 'fetchone_row', AsyncMock(return_value={'source': source})))
        stack.enter_context(patch('hippo.media_settings.get_media_settings', AsyncMock(return_value=SETTINGS)))
        stack.enter_context(patch.object(avatar_module, 'MPClient', (client or self._Client)(seen)))
        return stack.enter_context(patch.object(avatar_module, '_store_avatar', AsyncMock()))

    async def test_the_avatar_is_fetched_through_the_account_sources_exit(self) -> None:
        seen = {'order': [], 'response': _response()}

        with ExitStack() as stack:
            store = self._patch_all(stack, seen)
            result = await _fetch_and_cache_avatar(SimpleNamespace(), 'youtube:UC123', 'https://yt3/x=s88.jpg')

        self.assertEqual((b'avatar-bytes', 'image/png'), result)
        self.assertEqual('http://youtube:2', seen['proxy'])
        self.assertEqual(10.0, seen['timeout'])
        self.assertEqual('https://yt3/x=s88.jpg', seen['url'])
        store.assert_awaited_once()
        self.assertEqual('https://yt3/x=s88.jpg', store.await_args.kwargs['avatar_url'])

    async def test_the_event_loop_keeps_running_while_the_avatar_is_in_flight(self) -> None:
        seen = {'order': [], 'response': _response()}
        ran: list[str] = []

        async def other() -> None:
            await asyncio.sleep(0.01)
            ran.append('other')

        with ExitStack() as stack:
            self._patch_all(stack, seen)
            task = asyncio.create_task(other())
            await _fetch_and_cache_avatar(SimpleNamespace(), 'youtube:UC123', 'https://yt3/x.jpg')
            await task

        # The fetch yielded instead of doing blocking I/O: the other task ran during it.
        self.assertEqual(['other'], ran)

    async def test_a_non_200_answer_is_not_an_error(self) -> None:
        seen = {'order': [], 'response': _response(status=404, body=b'')}

        with ExitStack() as stack:
            store = self._patch_all(stack, seen)
            result = await _fetch_and_cache_avatar(SimpleNamespace(), 'youtube:UC123', 'https://yt3/missing.jpg')

        self.assertIsNone(result)
        store.assert_not_awaited()

    async def test_a_failed_fetch_is_logged_and_returns_none(self) -> None:
        seen = {'order': [], 'response': _response()}

        class _Broken(self._Client):
            async def get(self, url: str, *, headers: dict | None = None) -> httpx.Response:
                raise httpx.ConnectError('no route to host')

        with ExitStack() as stack:
            self._patch_all(stack, seen, client=_Broken)
            warning = stack.enter_context(patch.object(avatar_module.logger, 'warning'))
            result = await _fetch_and_cache_avatar(SimpleNamespace(), 'youtube:UC123', 'https://yt3/x.jpg')

        self.assertIsNone(result)
        warning.assert_called_once()


if __name__ == '__main__':
    unittest.main()
