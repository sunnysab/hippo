"""Whether a stored cookie still works, and what the answer may contain.

The value of this feature is the answer — "logged in as X" or "B站 says nobody is
logged in" — so the tests pin the answer for each shape the sources can give, and
pin that the credential itself never appears in it.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.api.routers import source as source_api
from hippo.sources.bilibili import BilibiliError
from hippo.sources.probe import probe_credential
from hippo.sources.youtube import YoutubeError

#: 出现在任何返回值里都算泄漏。
SENTINEL = 'SENTINEL-SESSDATA-4a1b'


def _raising(error: BaseException):
    """A callable that raises ``error`` — side_effect needs a raiser, not a value."""

    def boom(*args, **kwargs):
        raise error

    return boom


class BilibiliProbeTest(unittest.IsolatedAsyncioTestCase):
    async def _probe(self, nav):
        client = SimpleNamespace(nav=AsyncMock(side_effect=nav), aclose=AsyncMock())
        with patch('hippo.sources.probe.BilibiliClient', return_value=client):
            return await probe_credential(source='bilibili', cookie=SENTINEL)

    async def test_a_logged_in_session_names_the_account(self) -> None:
        probe = await self._probe(lambda: {'code': 0, 'data': {'isLogin': True, 'uname': '某位观众'}})

        self.assertTrue(probe.ok)
        self.assertEqual('某位观众', probe.account)
        self.assertNotIn(SENTINEL, probe.detail)

    async def test_an_expired_cookie_is_reported_as_not_logged_in(self) -> None:
        # -101 是 B站 对「这个 Cookie 不管用」的回答，不是异常。
        probe = await self._probe(lambda: {'code': -101, 'data': {'isLogin': False}})

        self.assertFalse(probe.ok)
        self.assertIn('未登录', probe.detail)
        self.assertNotIn(SENTINEL, probe.detail)

    async def test_risk_control_keeps_its_own_explanation(self) -> None:
        probe = await self._probe(_raising(BilibiliError('B站风控拦截：投稿列表需要有效的 SESSDATA')))

        self.assertFalse(probe.ok)
        self.assertIn('风控', probe.detail)

    async def test_an_unexpected_failure_does_not_raise_or_leak(self) -> None:
        probe = await self._probe(_raising(RuntimeError(f'boom {SENTINEL}')))

        self.assertFalse(probe.ok)
        # 连异常里的 cookie 都不能带出来。
        self.assertNotIn(SENTINEL, probe.detail)


class YoutubeProbeTest(unittest.IsolatedAsyncioTestCase):
    async def _probe(self, run):
        with patch('hippo.sources.probe.run_ytdlp', new=AsyncMock(side_effect=run)):
            return await probe_credential(source='youtube', cookie=SENTINEL)

    async def test_a_non_empty_subscription_list_means_the_login_works(self) -> None:
        probe = await self._probe(lambda *a, **k: '{"id": "abc", "title": "某人的订阅"}')

        self.assertTrue(probe.ok)
        self.assertIn('订阅', probe.detail)

    async def test_an_empty_list_is_a_refusal_not_a_success(self) -> None:
        # 未登录时 YouTube 不回错误，只给一个空列表——这就是它说「我不认识你」的方式。
        probe = await self._probe(lambda *a, **k: '')

        self.assertFalse(probe.ok)
        self.assertIn('空的', probe.detail)

    async def test_the_probe_asks_a_page_that_needs_a_login(self) -> None:
        run = AsyncMock(return_value='{"id": "abc"}')

        with patch('hippo.sources.probe.run_ytdlp', new=run):
            await probe_credential(source='youtube', cookie=SENTINEL)

        self.assertIn('/feed/subscriptions', run.call_args.args[0][-1])

    async def test_the_bot_check_is_reported_as_a_login_problem(self) -> None:
        error = YoutubeError(
            'yt-dlp 失败（退出码 1）：ERROR: [youtube] x: Sign in to confirm you’re not a bot.'
        )

        probe = await self._probe(_raising(error))

        self.assertFalse(probe.ok)
        self.assertIn('登录', probe.detail)
        self.assertNotIn(SENTINEL, probe.detail)

    async def test_the_cookie_is_handed_to_yt_dlp(self) -> None:
        run = AsyncMock(return_value='{"title": "x"}')

        with patch('hippo.sources.probe.run_ytdlp', new=run):
            await probe_credential(source='youtube', cookie=SENTINEL, proxy='http://proxy:1')

        self.assertEqual(SENTINEL, run.call_args.kwargs['cookies'])
        self.assertEqual('http://proxy:1', run.call_args.kwargs['proxy'])


def _user(role: str = 'admin') -> SimpleNamespace:
    return SimpleNamespace(id=1, username='u', role=role)


def _storage(credentials: dict | None = None) -> SimpleNamespace:
    store = dict(credentials or {})

    async def get(user_id, source):
        return store.get(source)

    return SimpleNamespace(credentials=SimpleNamespace(get=get), transaction=lambda: None)


class CredentialTestEndpointTest(unittest.IsolatedAsyncioTestCase):
    """The endpoint picks a credential, says which one, and never returns it."""

    def _patch(self, settings: dict, probe):
        return (
            patch('hippo.api.routers.source.get_media_settings', new=AsyncMock(return_value=settings)),
            patch('hippo.api.routers.source.probe_credential', new=AsyncMock(return_value=probe)),
        )

    async def test_the_users_own_credential_wins_over_the_instance_one(self) -> None:
        probe = SimpleNamespace(ok=True, detail='已登录', account='我')
        settings_patch, probe_patch = self._patch({'bilibili_cookie': 'instance-cookie'}, probe)

        with settings_patch, probe_patch as mocked:
            answer = await source_api.test_credential(
                source='bilibili', scope='user', user=_user(), storage=_storage({'bilibili': SENTINEL})
            )

        self.assertEqual('user', answer['credential'])
        self.assertTrue(answer['ok'])
        self.assertEqual(SENTINEL, mocked.call_args.kwargs['cookie'])
        self.assertNotIn(SENTINEL, str(answer))

    async def test_it_falls_back_to_the_instance_credential_and_says_so(self) -> None:
        probe = SimpleNamespace(ok=True, detail='已登录', account='')
        settings_patch, probe_patch = self._patch({'bilibili_cookie': 'instance-cookie'}, probe)

        with settings_patch, probe_patch as mocked:
            answer = await source_api.test_credential(
                source='bilibili', scope='user', user=_user(), storage=_storage()
            )

        self.assertEqual('instance', answer['credential'])
        self.assertEqual('instance-cookie', mocked.call_args.kwargs['cookie'])

    async def test_no_credential_anywhere_is_answered_without_calling_out(self) -> None:
        settings_patch, probe_patch = self._patch({}, SimpleNamespace(ok=True, detail='', account=''))

        with settings_patch, probe_patch as mocked:
            answer = await source_api.test_credential(
                source='bilibili', scope='user', user=_user(), storage=_storage()
            )

        self.assertFalse(answer['ok'])
        self.assertEqual('none', answer['credential'])
        mocked.assert_not_awaited()

    async def test_testing_the_instance_credential_is_admin_only(self) -> None:
        settings_patch, probe_patch = self._patch({}, SimpleNamespace(ok=True, detail='', account=''))

        with settings_patch, probe_patch, self.assertRaises(source_api.ApiError):
            await source_api.test_credential(
                source='youtube', scope='instance', user=_user(role='user'), storage=_storage()
            )

    async def test_an_unknown_source_is_refused(self) -> None:
        settings_patch, probe_patch = self._patch({}, SimpleNamespace(ok=True, detail='', account=''))

        with settings_patch, probe_patch, self.assertRaises(source_api.ApiError):
            await source_api.test_credential(
                source='weixin', scope='user', user=_user(), storage=_storage()
            )


if __name__ == '__main__':
    unittest.main()
