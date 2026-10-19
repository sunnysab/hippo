"""Subscribing by URL, and per-user credentials.

The interesting behaviour is what the API refuses (a non-URL, a disabled instance),
and that a subscription is idempotent while a credential is never echoed back.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from hippo.api.deps import current_user, get_storage
from hippo.api.routers import source as source_api
from hippo.api.schemas.source import CredentialRequest, SubscribeRequest
from hippo.exceptions import ApiError
from hippo.models import AccountCredential
from hippo.sources.bilibili import BilibiliError
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

    def test_a_wechat_article_names_its_account(self) -> None:
        self.assertEqual('weixin', detect_source('https://mp.weixin.qq.com/s/RpLLRPedSrdgBZHWXQxJbQ'))
        self.assertEqual(
            'weixin',
            detect_source('https://mp.weixin.qq.com/s?__biz=MzU2&mid=1&idx=1&sn=abc'),
        )

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


def _storage(
    *, subscribed: bool = False, credentials: dict | None = None, transactions: list | None = None
) -> SimpleNamespace:
    accounts = AsyncMock()
    accounts.upsert_account = AsyncMock(side_effect=lambda account: account.model_copy(update={'group_id': 1}))
    if subscribed:
        accounts.get_account = AsyncMock(return_value=SimpleNamespace(biz='x'))
        accounts.get_account_detail = AsyncMock(return_value={'biz': 'podcast:abc'})
    else:
        accounts.get_account = AsyncMock(side_effect=LookupError('nope'))
        accounts.get_account_detail = AsyncMock(side_effect=LookupError('not followed'))

    def transaction():
        if transactions is not None:
            transactions.append(1)
        return _NullTransaction()

    articles = AsyncMock()
    articles.find_biz_by_link = AsyncMock(return_value=None)

    return SimpleNamespace(
        transaction=transaction,
        accounts=accounts,
        articles=articles,
        groups=AsyncMock(),
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

    async def test_a_channel_in_the_catalogue_is_not_already_followed_by_every_user(self) -> None:
        sources, _ = _sources(_INFO)
        storage = _storage()
        storage.accounts.get_account.return_value = SimpleNamespace(biz=_INFO.biz)
        storage.accounts.get_account.side_effect = None
        with (
            patch('hippo.api.routers.source.MediaSources', sources),
            patch('hippo.api.routers.source.get_media_settings', new=AsyncMock(return_value={'enabled': True})),
        ):
            result = await source_api.preview_source(url='https://feed.example/rss', user=_user(42), storage=storage)

        self.assertFalse(result['subscribed'])
        storage.accounts.get_account_detail.assert_awaited_once_with(_INFO.biz, user_id=42)
        storage.accounts.get_account.assert_not_awaited()

    async def test_a_preview_without_an_avatar_survives_response_validation(self) -> None:
        sources, _ = _sources(_INFO)
        storage = _storage()
        app = FastAPI()
        app.include_router(source_api.router, prefix='/api')
        app.dependency_overrides[current_user] = _user
        app.dependency_overrides[get_storage] = lambda: storage
        with (
            patch('hippo.api.routers.source.MediaSources', sources),
            patch('hippo.api.routers.source.get_media_settings', new=AsyncMock(return_value={'enabled': True})),
        ):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.get('/api/source/preview', params={'url': 'https://feed.example/rss'})

        self.assertEqual(200, response.status_code)
        self.assertIsNone(response.json()['avatar_url'])
        self.assertFalse(response.json()['subscribed'])

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

    async def test_an_adapter_error_is_a_bad_gateway_carrying_its_message(self) -> None:
        # A SourceError is written for the operator, so its sentence is served.
        sources, _ = _sources(error=BilibiliError('B站风控拦截：投稿列表需要有效的 SESSDATA'))
        with (
            patch('hippo.api.routers.source.MediaSources', sources),
            patch('hippo.api.routers.source.get_media_settings', new=AsyncMock(return_value={'enabled': True})),
            self.assertRaises(ApiError) as ctx,
        ):
            await source_api.preview_source(url='https://space.bilibili.com/1', user=_user(), storage=_storage())

        self.assertEqual(502, ctx.exception.status)
        self.assertEqual('source.parse_failed', ctx.exception.code)
        self.assertIn('风控', str(ctx.exception))

    async def test_an_internal_defect_is_a_bad_gateway_without_its_detail(self) -> None:
        """A plain exception is our own bug: log it, do not hand it to the caller."""
        error = RuntimeError('psql://user:secret@host/db')
        sources, _ = _sources(error=error)
        with (
            patch('hippo.api.routers.source.MediaSources', sources),
            patch('hippo.api.routers.source.get_media_settings', new=AsyncMock(return_value={'enabled': True})),
            self.assertRaises(ApiError) as ctx,
        ):
            await source_api.preview_source(url='https://space.bilibili.com/1', user=_user(), storage=_storage())

        self.assertEqual(502, ctx.exception.status)
        self.assertEqual('解析失败', str(ctx.exception))
        self.assertNotIn('secret', str(ctx.exception))
        # Chained, not swallowed: the traceback still reaches the log.
        self.assertIs(error, ctx.exception.__cause__)

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
        transactions: list[int] = []
        storage = _storage(transactions=transactions)
        with (
            patch('hippo.api.routers.source.MediaSources', sources),
            patch('hippo.api.routers.source.get_media_settings', new=AsyncMock(return_value={'enabled': True})),
            patch('hippo.api.routers.source.ensure_default_group', new=AsyncMock(return_value=SimpleNamespace(id=1))),
        ):
            result = await source_api.subscribe_source(
                request=_request(),
                body=SubscribeRequest(url='https://feed.example/rss'),
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
        job = storage.sync_jobs.create_job.call_args.kwargs
        self.assertEqual(['podcast:abc'], job['biz_list'])
        # A subscription is its own trigger, so it never collides with the
        # one-active-manual-per-user unique index.
        self.assertEqual('subscription', job['trigger_type'])
        self.assertNotIn('dedupe', job)
        self.assertEqual('podcast', job['source'])
        # Recorded requester is what makes the round visible to its user.
        self.assertEqual(1, job['requester_user_id'])
        # The subscription and its first round stay in one transaction.
        self.assertEqual([1], transactions)

    async def test_a_second_subscription_is_not_deduped_away(self) -> None:
        sources, _ = _sources(_INFO)
        storage = _storage()
        with (
            patch('hippo.api.routers.source.MediaSources', sources),
            patch('hippo.api.routers.source.get_media_settings', new=AsyncMock(return_value={'enabled': True})),
            patch('hippo.api.routers.source.ensure_default_group', new=AsyncMock(return_value=SimpleNamespace(id=1))),
        ):
            for _ in range(2):
                await source_api.subscribe_source(
                    request=_request(),
                    body=SubscribeRequest(url='https://feed.example/rss'),
                    user=_user(),
                    storage=storage,
                )

        triggers = [call.kwargs['trigger_type'] for call in storage.sync_jobs.create_job.call_args_list]
        self.assertEqual(['subscription', 'subscription'], triggers)

    async def test_a_foreign_group_is_refused_before_resolving_or_writing(self) -> None:
        storage = _storage()
        storage.groups.get_group.side_effect = LookupError('not owned')
        with (
            patch('hippo.api.routers.source._media_sources', new=AsyncMock()) as media_sources,
            self.assertRaises(ApiError) as caught,
        ):
            await source_api.subscribe_source(
                request=_request(),
                body=SubscribeRequest(url='https://feed.example/rss', group_id=8),
                user=_user(42),
                storage=storage,
            )

        self.assertEqual(404, caught.exception.status)
        storage.groups.get_group.assert_awaited_once_with(8, user_id=42)
        media_sources.assert_not_awaited()
        storage.accounts.upsert_account.assert_not_awaited()
        storage.subscriptions.upsert.assert_not_awaited()
        storage.sync_jobs.create_job.assert_not_awaited()

    async def test_a_channel_can_be_added_to_the_callers_own_group(self) -> None:
        sources, _ = _sources(_INFO)
        storage = _storage()
        with (
            patch('hippo.api.routers.source.MediaSources', sources),
            patch('hippo.api.routers.source.get_media_settings', new=AsyncMock(return_value={'enabled': True})),
        ):
            result = await source_api.subscribe_source(
                request=_request(),
                body=SubscribeRequest(url='https://feed.example/rss', group_id=8),
                user=_user(42),
                storage=storage,
            )

        self.assertEqual(8, result['group_id'])
        storage.groups.get_group.assert_awaited_once_with(8, user_id=42)
        storage.subscriptions.upsert.assert_awaited_once_with(42, _INFO.biz, group_id=8)

    async def test_a_blank_url_is_a_bad_request(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            await source_api.subscribe_source(
                request=_request(), body=SubscribeRequest(url='   '), user=_user(), storage=_storage()
            )

        self.assertEqual(400, ctx.exception.status)

    def test_a_subscription_needs_a_url_at_all(self) -> None:
        # The body is declared with a url, so an empty one never reaches the route.
        with self.assertRaises(ValidationError):
            SubscribeRequest.model_validate({})


_ARTICLE_URL = 'https://mp.weixin.qq.com/s/RpLLRPedSrdgBZHWXQxJbQ'


def _publisher(biz: str = 'MzU2MDI3MDk5NQ==', gh_id: str = 'gh_3eb1a1b60c4c') -> SimpleNamespace:
    return SimpleNamespace(
        biz=biz,
        gh_id=gh_id,
        nickname='TopView',
        alias='TopView_Skyfall',
        avatar_url='http://mmbiz.qpic.cn/mmbiz_png/example',
    )


class WeixinArticleTest(unittest.IsolatedAsyncioTestCase):
    """Pasting an article subscribes to whoever wrote it."""

    async def test_an_archived_article_names_its_account_without_asking_the_daemon(self) -> None:
        storage = _storage()
        storage.articles.find_biz_by_link = AsyncMock(return_value='MzU2MDI3MDk5NQ==')
        storage.accounts.get_account = AsyncMock(return_value=SimpleNamespace(
            biz='MzU2MDI3MDk5NQ==', nickname='TopView', gh_id='gh_3eb1a1b60c4c', alias='TopView_Skyfall',
        ))
        with patch('hippo.api.routers.source.WeixinDaemon') as daemon:
            result = await source_api.preview_source(url=_ARTICLE_URL, user=_user(), storage=storage)

        self.assertEqual('weixin', result['source'])
        self.assertEqual('TopView', result['nickname'])
        self.assertEqual('/api/account/MzU2MDI3MDk5NQ==/avatar', result['avatar_url'])
        storage.articles.find_biz_by_link.assert_awaited_once_with(
            _ARTICLE_URL, slug='RpLLRPedSrdgBZHWXQxJbQ',
        )
        daemon.assert_not_called()

    async def test_an_unknown_article_is_resolved_through_the_daemon(self) -> None:
        storage = _storage()
        body = SimpleNamespace(raw_json='{"bizuin": "MzU2MDI3MDk5NQ==", "user_name": "gh_3eb1a1b60c4c",'
                                        ' "nick_name": "TopView", "round_head_img": "http://img/x"}')
        body.nick_name = 'TopView'
        source = AsyncMock()
        source.resolve_article = AsyncMock(return_value=body)
        source.__aenter__ = AsyncMock(return_value=source)
        source.__aexit__ = AsyncMock(return_value=False)
        with (
            patch('hippo.api.routers.source.WeixinDaemon', return_value=source),
            patch('hippo.api.routers.source._upsert_avatar_url', new=AsyncMock()) as cache_avatar,
        ):
            result = await source_api.preview_source(url=_ARTICLE_URL, user=_user(), storage=storage)

        self.assertEqual('MzU2MDI3MDk5NQ==', result['biz'])
        self.assertEqual('TopView', result['nickname'])
        # 头像是微信图床，直连会被防盗链挡住：缓存起来走代理。
        cache_avatar.assert_awaited_once_with(storage, 'MzU2MDI3MDk5NQ==', 'http://img/x')
        self.assertEqual('/api/account/MzU2MDI3MDk5NQ==/avatar', result['avatar_url'])

    async def test_an_article_without_a_publisher_is_a_bad_gateway(self) -> None:
        storage = _storage()
        body = SimpleNamespace(raw_json='{"user_name": "gh_3eb1a1b60c4c"}', nick_name='')
        source = AsyncMock()
        source.resolve_article = AsyncMock(return_value=body)
        source.__aenter__ = AsyncMock(return_value=source)
        source.__aexit__ = AsyncMock(return_value=False)
        with (
            patch('hippo.api.routers.source.WeixinDaemon', return_value=source),
            self.assertRaises(ApiError) as ctx,
        ):
            await source_api.preview_source(url=_ARTICLE_URL, user=_user(), storage=storage)

        self.assertEqual(502, ctx.exception.status)

    async def test_subscribing_by_article_writes_the_account_and_follows_it(self) -> None:
        storage = _storage()
        storage.articles.find_biz_by_link = AsyncMock(return_value='MzU2MDI3MDk5NQ==')
        storage.accounts.get_account = AsyncMock(return_value=SimpleNamespace(
            biz='MzU2MDI3MDk5NQ==', nickname='TopView', gh_id='gh_3eb1a1b60c4c', alias='TopView_Skyfall',
        ))
        with patch(
            'hippo.api.routers.source.ensure_default_group',
            new=AsyncMock(return_value=SimpleNamespace(id=1)),
        ):
            result = await source_api.subscribe_source(
                request=_request(),
                body=SubscribeRequest(url=_ARTICLE_URL),
                user=_user(),
                storage=storage,
            )

        self.assertEqual('weixin', result['source'])
        self.assertEqual(1, result['group_id'])
        account: AccountCredential = storage.accounts.upsert_account.call_args.args[0]
        self.assertEqual('MzU2MDI3MDk5NQ==', account.biz)
        # The gh_ is what the list RPC needs, so it is stored with the account.
        self.assertEqual('gh_3eb1a1b60c4c', account.gh_id)
        storage.subscriptions.upsert.assert_awaited_once_with(1, 'MzU2MDI3MDk5NQ==', group_id=1)

    async def test_weixin_subscription_does_not_need_the_media_instance(self) -> None:
        """Turning transcription off must not make article links unsubscribable."""
        storage = _storage()
        storage.articles.find_biz_by_link = AsyncMock(return_value='MzU2MDI3MDk5NQ==')
        storage.accounts.get_account = AsyncMock(return_value=SimpleNamespace(
            biz='MzU2MDI3MDk5NQ==', nickname='TopView', gh_id='gh_3eb1a1b60c4c', alias=None,
        ))
        with (
            patch('hippo.api.routers.source._media_sources', new=AsyncMock()) as media_sources,
            patch(
                'hippo.api.routers.source.ensure_default_group',
                new=AsyncMock(return_value=SimpleNamespace(id=1)),
            ),
        ):
            await source_api.subscribe_source(
                request=_request(),
                body=SubscribeRequest(url=_ARTICLE_URL),
                user=_user(),
                storage=storage,
            )

        media_sources.assert_not_awaited()


class CredentialTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_credential_is_never_echoed(self) -> None:
        storage = _storage()

        await source_api.set_credential(
            source='bilibili', body=CredentialRequest(secret='SESSDATA=abc'), user=_user(), storage=storage
        )
        listed = await source_api.list_credentials(user=_user(), storage=storage)

        self.assertEqual({'bilibili': {'configured': True}, 'youtube': {'configured': False}}, listed)
        self.assertNotIn('secret', str(listed))

    async def test_an_empty_secret_clears_it(self) -> None:
        storage = _storage(credentials={'bilibili': 'SESSDATA=abc'})

        await source_api.set_credential(
            source='bilibili', body=CredentialRequest(secret='  '), user=_user(), storage=storage
        )

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
            await source_api.set_credential(
                source='weixin', body=CredentialRequest(secret='x'), user=_user(), storage=_storage()
            )

    async def test_a_bilibili_cookie_without_sessdata_is_refused(self) -> None:
        # 没有 SESSDATA 的「cookie」过不了 B站的风控，早拒绝比晚 412 好。
        with self.assertRaises(ApiError) as ctx:
            await source_api.set_credential(
                source='bilibili', body=CredentialRequest(secret='buvid3=abc'), user=_user(), storage=_storage()
            )

        self.assertIn('SESSDATA', str(ctx.exception))

    async def test_a_cookie_header_is_flattened_before_it_is_stored(self) -> None:
        # 换行会被 HTTP 客户端当成头注入拒绝，落库前压成一行。
        storage = _storage()

        await source_api.set_credential(
            source='bilibili',
            body=CredentialRequest(secret='SESSDATA=abc;\n  bili_jct=def\n'),
            user=_user(),
            storage=storage,
        )

        self.assertEqual('SESSDATA=abc; bili_jct=def', await storage.credentials.get(1, 'bilibili'))

    async def test_a_netscape_cookie_file_is_accepted_for_youtube(self) -> None:
        storage = _storage()
        cookies = '# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1800000000\tSID\tabc\n'

        await source_api.set_credential(
            source='youtube', body=CredentialRequest(secret=cookies), user=_user(), storage=storage
        )

        self.assertEqual(cookies.strip(), await storage.credentials.get(1, 'youtube'))

    async def test_an_html_page_is_refused_for_both_sources(self) -> None:
        html = '<!DOCTYPE html><html><body>Sign in</body></html>'

        for source in ('bilibili', 'youtube'):
            with self.subTest(source=source), self.assertRaises(ApiError):
                await source_api.set_credential(
                    source=source, body=CredentialRequest(secret=html), user=_user(), storage=_storage()
                )

    async def test_an_oversized_credential_is_refused(self) -> None:
        huge = 'SESSDATA=' + 'a' * (MAX_CREDENTIAL_BYTES + 1)

        with self.assertRaises(ApiError) as ctx:
            await source_api.set_credential(
                source='bilibili', body=CredentialRequest(secret=huge), user=_user(), storage=_storage()
            )

        self.assertIn('过大', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
