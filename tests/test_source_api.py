"""Subscribing by URL, and per-user credentials.

The interesting behaviour is what the API refuses (a non-URL, a disabled instance),
and that a subscription is idempotent while a credential is never echoed back.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.api.routers import source as source_api
from hippo.exceptions import ApiError
from hippo.models import AccountCredential
from hippo.sources.credentials import MAX_CREDENTIAL_BYTES
from hippo.sources.detect import detect_source
from hippo.sources.ingest import ChannelInfo


class DetectSourceTest(unittest.TestCase):
    def test_a_bilibili_video(self) -> None:
        self.assertEqual('bilibili', detect_source('https://www.bilibili.com/video/BV1gHps6yEHL'))

    def test_a_bilibili_short_link(self) -> None:
        self.assertEqual('bilibili', detect_source('https://b23.tv/abc123'))

    def test_a_youtube_short_link(self) -> None:
        self.assertEqual('youtube', detect_source('https://youtu.be/WrCjAAl9okA'))

    def test_a_channel_handle(self) -> None:
        self.assertEqual('youtube', detect_source('https://www.youtube.com/@Fireship'))

    def test_anything_else_http_is_tried_as_a_feed(self) -> None:
        self.assertEqual('podcast', detect_source('https://feeds.megaphone.fm/darknetdiaries'))

    def test_a_non_url_is_refused(self) -> None:
        self.assertIsNone(detect_source('feeds.megaphone.fm/darknetdiaries'))
        self.assertIsNone(detect_source(''))


class _NullTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _storage(*, subscribed: bool = False, credentials: dict | None = None) -> SimpleNamespace:
    accounts = AsyncMock()
    accounts.upsert_account = AsyncMock(side_effect=lambda account: account.model_copy(update={'group_id': 1}))
    if subscribed:
        accounts.get_account = AsyncMock(return_value=SimpleNamespace(biz='x'))
    else:
        accounts.get_account = AsyncMock(side_effect=LookupError('nope'))
    return SimpleNamespace(
        transaction=lambda: _NullTransaction(),
        accounts=accounts,
        subscriptions=AsyncMock(),
        sync_jobs=AsyncMock(),
        audit=AsyncMock(),
        credentials=AsyncMock(**_credential_methods(credentials or {})),
    )


def _credential_methods(seed: dict) -> dict:
    store = dict(seed)

    async def get(user_id, source):
        return store.get(source)

    async def configured(user_id):
        return {source: True for source in store if store[source]}

    async def set_(user_id, source, secret):
        if secret.strip():
            store[source] = secret
        else:
            store.pop(source, None)

    async def delete(user_id, source):
        store.pop(source, None)

    return {'get': get, 'configured': configured, 'set': set_, 'delete': delete}


def _user(user_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, username='u', role='admin')


def _request() -> SimpleNamespace:
    return SimpleNamespace(headers={}, client=SimpleNamespace(host='127.0.0.1'))


def _sources(info: ChannelInfo | None = None, *, error: Exception | None = None):
    preview = AsyncMock(side_effect=error) if error else AsyncMock(return_value=info)

    class _MediaSources:
        def __init__(self, **_kwargs) -> None:
            self.preview = preview
            self.aclose = AsyncMock()

    return _MediaSources, preview


_INFO = ChannelInfo(biz='podcast:abc', nickname='一档播客', source_key='https://feed.example/rss')


class PreviewTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_feed_url_resolves_to_a_channel(self) -> None:
        sources, _ = _sources(_INFO)
        storage = _storage()
        with (
            patch('hippo.api.routers.source.MediaSources', sources),
            patch('hippo.api.routers.source.get_media_settings', new=AsyncMock(return_value={'enabled': True})),
        ):
            result = await source_api.preview_source(url='https://feed.example/rss', user=_user(), storage=storage)

        self.assertEqual('podcast', result['source'])
        self.assertEqual('一档播客', result['nickname'])
        self.assertFalse(result['subscribed'])

    async def test_an_already_subscribed_channel_says_so(self) -> None:
        sources, _ = _sources(_INFO)
        storage = _storage(subscribed=True)
        with (
            patch('hippo.api.routers.source.MediaSources', sources),
            patch('hippo.api.routers.source.get_media_settings', new=AsyncMock(return_value={'enabled': True})),
        ):
            result = await source_api.preview_source(url='https://feed.example/rss', user=_user(), storage=storage)

        self.assertTrue(result['subscribed'])

    async def test_a_non_url_is_a_bad_request(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            await source_api.preview_source(url='not a url', user=_user(), storage=_storage())

        self.assertEqual(400, ctx.exception.status)

    async def test_a_disabled_instance_refuses_before_calling_out(self) -> None:
        sources, preview = _sources(_INFO)
        with (
            patch('hippo.api.routers.source.MediaSources', sources),
            patch('hippo.api.routers.source.get_media_settings', new=AsyncMock(return_value={'enabled': False})),
            self.assertRaises(ApiError),
        ):
            await source_api.preview_source(url='https://feed.example/rss', user=_user(), storage=_storage())

        preview.assert_not_awaited()

    async def test_an_upstream_failure_is_a_bad_gateway(self) -> None:
        sources, _ = _sources(error=RuntimeError('风控拦截'))
        with (
            patch('hippo.api.routers.source.MediaSources', sources),
            patch('hippo.api.routers.source.get_media_settings', new=AsyncMock(return_value={'enabled': True})),
            self.assertRaises(ApiError) as ctx,
        ):
            await source_api.preview_source(url='https://space.bilibili.com/1', user=_user(), storage=_storage())

        self.assertEqual(502, ctx.exception.status)
        self.assertIn('风控', str(ctx.exception))

    async def test_a_value_error_is_a_bad_request(self) -> None:
        sources, _ = _sources(error=ValueError('这不像是一个 B站 链接'))
        with (
            patch('hippo.api.routers.source.MediaSources', sources),
            patch('hippo.api.routers.source.get_media_settings', new=AsyncMock(return_value={'enabled': True})),
            self.assertRaises(ApiError) as ctx,
        ):
            await source_api.preview_source(url='https://www.bilibili.com/x', user=_user(), storage=_storage())

        self.assertEqual(400, ctx.exception.status)


class SubscribeTest(unittest.IsolatedAsyncioTestCase):
    async def test_subscribing_writes_the_catalogue_and_queues_a_round(self) -> None:
        sources, _ = _sources(_INFO)
        storage = _storage()
        with (
            patch('hippo.api.routers.source.MediaSources', sources),
            patch('hippo.api.routers.source.get_media_settings', new=AsyncMock(return_value={'enabled': True})),
            patch('hippo.api.routers.source.ensure_default_group', new=AsyncMock(return_value=SimpleNamespace(id=1))),
        ):
            result = await source_api.subscribe_source(
                request=_request(),
                body={'url': 'https://feed.example/rss'},
                user=_user(),
                storage=storage,
            )

        self.assertEqual('podcast:abc', result['biz'])
        self.assertEqual('podcast', result['source'])
        account: AccountCredential = storage.accounts.upsert_account.call_args.args[0]
        self.assertEqual('podcast', account.source)
        # alias 存的是来源内的标识，biz 是加了命名空间的那个
        self.assertEqual('https://feed.example/rss', account.alias)
        storage.subscriptions.upsert.assert_awaited()
        self.assertEqual(['podcast:abc'], storage.sync_jobs.create_job.call_args.kwargs['biz_list'])

    async def test_a_missing_url_is_a_bad_request(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            await source_api.subscribe_source(request=_request(), body={}, user=_user(), storage=_storage())

        self.assertEqual(400, ctx.exception.status)


class CredentialTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_credential_is_never_echoed(self) -> None:
        storage = _storage()

        await source_api.set_credential(
            source='bilibili', body={'secret': 'SESSDATA=abc'}, user=_user(), storage=storage
        )
        listed = await source_api.list_credentials(user=_user(), storage=storage)

        self.assertEqual(
            {'bilibili': {'configured': True}, 'youtube': {'configured': False}}, listed
        )
        self.assertNotIn('secret', str(listed))

    async def test_an_empty_secret_clears_it(self) -> None:
        storage = _storage(credentials={'bilibili': 'SESSDATA=abc'})

        await source_api.set_credential(source='bilibili', body={'secret': '  '}, user=_user(), storage=storage)

        self.assertEqual(
            {'bilibili': {'configured': False}, 'youtube': {'configured': False}},
            await source_api.list_credentials(user=_user(), storage=storage),
        )

    async def test_deleting_reports_it_as_gone(self) -> None:
        storage = _storage(credentials={'bilibili': 'SESSDATA=abc'})

        await source_api.delete_credential(source='bilibili', user=_user(), storage=storage)

        self.assertEqual(
            {'bilibili': {'configured': False}, 'youtube': {'configured': False}},
            await source_api.list_credentials(user=_user(), storage=storage),
        )

    async def test_an_unsupported_source_is_refused(self) -> None:
        with self.assertRaises(ApiError):
            await source_api.set_credential(source='weixin', body={}, user=_user(), storage=_storage())

    async def test_a_bilibili_cookie_without_sessdata_is_refused(self) -> None:
        # 没有 SESSDATA 的「cookie」过不了 B站的风控，早拒绝比晚 412 好。
        with self.assertRaises(ApiError) as ctx:
            await source_api.set_credential(
                source='bilibili', body={'secret': 'buvid3=abc'}, user=_user(), storage=_storage()
            )

        self.assertIn('SESSDATA', str(ctx.exception))

    async def test_a_cookie_header_is_flattened_before_it_is_stored(self) -> None:
        # 换行会被 HTTP 客户端当成头注入拒绝，落库前压成一行。
        storage = _storage()

        await source_api.set_credential(
            source='bilibili',
            body={'secret': 'SESSDATA=abc;\n  bili_jct=def\n'},
            user=_user(),
            storage=storage,
        )

        self.assertEqual('SESSDATA=abc; bili_jct=def', await storage.credentials.get(1, 'bilibili'))

    async def test_a_netscape_cookie_file_is_accepted_for_youtube(self) -> None:
        storage = _storage()
        cookies = '# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1800000000\tSID\tabc\n'

        await source_api.set_credential(source='youtube', body={'secret': cookies}, user=_user(), storage=storage)

        self.assertEqual(cookies.strip(), await storage.credentials.get(1, 'youtube'))

    async def test_an_html_page_is_refused_for_both_sources(self) -> None:
        html = '<!DOCTYPE html><html><body>Sign in</body></html>'

        for source in ('bilibili', 'youtube'):
            with self.subTest(source=source), self.assertRaises(ApiError):
                await source_api.set_credential(source=source, body={'secret': html}, user=_user(), storage=_storage())

    async def test_an_oversized_credential_is_refused(self) -> None:
        huge = 'SESSDATA=' + 'a' * (MAX_CREDENTIAL_BYTES + 1)

        with self.assertRaises(ApiError) as ctx:
            await source_api.set_credential(source='bilibili', body={'secret': huge}, user=_user(), storage=_storage())

        self.assertIn('过大', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
