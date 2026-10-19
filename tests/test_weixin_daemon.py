from __future__ import annotations

import asyncio
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.sources import SourceError
from hippo.sync_types import SyncStats
from hippo.weixin_daemon import (
    FetchedArticle,
    SessionExpiredError,
    WeixinSource,
    WeixinUnavailableError,
    classify_daemon_error,
    select_cover_url,
)
from hippo.weixin_ingest import WeixinArticleSync


class _Bot:
    async def call(self, action, params, *, timeout):
        assert action == 'get_article_bodies'
        return {
            'articles': [
                {
                    'url': params['urls'][0],
                    'short_link': 'https://mp.weixin.qq.com/s/slug',
                    'slug': 'slug',
                    'html': '<p>body</p>',
                }
            ],
            'errors': [
                {'urls': [params['urls'][1]], 'error': '6771 empty; 2594 returned no body'},
            ],
        }


class _NormalizingBot:
    """daemon 把长链上的 scene / #rd 去掉后再回答正文。"""

    async def call(self, action, params, *, timeout):
        assert action == 'get_article_bodies'
        return {
            'articles': [
                {
                    'url': 'http://mp.weixin.qq.com/s?__biz=Mz1&sn=abc123',
                    'short_link': 'https://mp.weixin.qq.com/s/slug-x',
                    'slug': 'slug-x',
                    'html': '<p>body</p>',
                }
            ],
            'errors': [],
        }


class _ListBot:
    def __init__(self, articles: list[dict]) -> None:
        self._articles = articles

    async def call(self, action, params, *, timeout):
        assert action == 'get_biz_articles'
        return {
            'biz': 'gh_1',
            'count': len(self._articles),
            'articles': self._articles,
            'next_offset': 'cur',
            'is_end': False,
        }


class WeixinSourceListTest(unittest.TestCase):
    def test_strips_nul_characters_that_postgres_would_reject(self) -> None:
        source = WeixinSource(auto_login=False)
        source._bot = _ListBot(
            [
                {
                    'title': 'a\x00b',
                    'digest': 'x\x00',
                    'url': 'https://mp.weixin.qq.com/s?__biz=Mz1&sn=abc',
                    'publish_time': 1,
                    'cover_url': 'https://example.invalid/c\x00.png',
                }
            ]
        )

        listed = asyncio.run(source.list_articles('gh_1', 'Mz1'))

        self.assertEqual(1, len(listed.items))
        payload = listed.items[0].payload
        self.assertEqual('ab', payload['title'])
        self.assertEqual('x', payload['digest'])
        self.assertEqual('https://example.invalid/c.png', payload['cover_url'])
        self.assertEqual('cur', listed.next_offset)


class CoverSelectionTest(unittest.TestCase):
    """One cover rule for the list, the article detail and the push event."""

    def test_the_wide_crop_wins_over_the_main_image(self) -> None:
        variants = [
            {'ratio': 'main', 'url': 'https://cdn/main.png'},
            {'ratio': '1:1', 'url': 'https://cdn/square.png'},
            {'ratio': '16:9', 'url': 'https://cdn/wide.png'},
        ]

        self.assertEqual('https://cdn/wide.png', select_cover_url(variants, 'https://cdn/main.png'))

    def test_without_a_wide_crop_the_main_image_is_used(self) -> None:
        variants = [{'ratio': 'main', 'url': 'https://cdn/main.png'}, {'ratio': '1:1', 'url': 'https://cdn/square.png'}]

        self.assertEqual('https://cdn/main.png', select_cover_url(variants, 'https://cdn/main.png'))

    def test_an_unlabelled_variant_counts_as_the_main_image(self) -> None:
        self.assertEqual('https://cdn/main.png', select_cover_url([{'url': 'https://cdn/main.png'}], None))

    def test_a_variant_carrying_no_url_is_skipped(self) -> None:
        variants = [
            {'ratio': '16:9', 'url': ''},
            {'ratio': '16:9', 'url': None},
            {'ratio': 'main', 'url': 'https://cdn/main.png'},
        ]

        self.assertEqual('https://cdn/main.png', select_cover_url(variants, None))

    def test_a_repeated_variant_does_not_change_the_answer(self) -> None:
        variants = [
            {'ratio': 'main', 'url': 'https://cdn/main.png'},
            {'ratio': 'main', 'url': 'https://cdn/main.png'},
            {'ratio': '16:9', 'url': 'https://cdn/wide.png'},
            {'ratio': '16:9', 'url': 'https://cdn/wide.png'},
        ]

        self.assertEqual('https://cdn/wide.png', select_cover_url(variants, None))

    def test_the_wide_crop_equal_to_the_main_key_is_still_that_image(self) -> None:
        # daemon 只在 URL 真的不同时才给出该比例的条目，同一个 URL 就是同一张图。
        variants = [{'ratio': 'main', 'url': 'https://cdn/same.png'}, {'ratio': '16:9', 'url': 'https://cdn/same.png'}]

        self.assertEqual('https://cdn/same.png', select_cover_url(variants, None))

    def test_an_older_daemon_single_image_is_kept(self) -> None:
        self.assertEqual('https://cdn/main.png', select_cover_url(None, 'https://cdn/main.png'))
        self.assertEqual('https://cdn/main.png', select_cover_url([], 'https://cdn/main.png'))
        self.assertEqual(
            'https://cdn/main.png', select_cover_url([{'ratio': '1:1', 'url': ''}], 'https://cdn/main.png')
        )

    def test_no_cover_at_all_is_none(self) -> None:
        self.assertIsNone(select_cover_url(None, None))
        self.assertIsNone(select_cover_url([], ''))
        self.assertIsNone(select_cover_url(['https://cdn/not-a-dict.png'], None))

    def test_the_list_payload_uses_the_wide_crop(self) -> None:
        source = WeixinSource(auto_login=False)
        source._bot = _ListBot(
            [
                {
                    'title': 'a',
                    'url': 'https://mp.weixin.qq.com/s?__biz=Mz1&sn=abc',
                    'cover_url': 'https://cdn/main.png',
                    'cover_urls': [
                        {'ratio': 'main', 'url': 'https://cdn/main.png'},
                        {'ratio': '16:9', 'url': 'https://cdn/wide.png'},
                    ],
                }
            ]
        )

        listed = asyncio.run(source.list_articles('gh_1', 'Mz1'))

        self.assertEqual('https://cdn/wide.png', listed.items[0].payload['cover_url'])

    def test_the_body_payload_uses_the_wide_crop(self) -> None:
        body = FetchedArticle.from_dict(
            {
                'short_link': 'https://mp.weixin.qq.com/s/a',
                'slug': 'a',
                'cover_url': 'https://cdn/main.png',
                'cover_urls': [
                    {'ratio': 'main', 'url': 'https://cdn/main.png'},
                    {'ratio': '16:9', 'url': 'https://cdn/wide.png'},
                ],
            }
        )

        self.assertEqual('https://cdn/wide.png', body.cover_url)

    def test_a_body_without_variants_keeps_the_daemon_image(self) -> None:
        body = FetchedArticle.from_dict({'slug': 'a', 'cover_url': 'https://cdn/main.png'})

        self.assertEqual('https://cdn/main.png', body.cover_url)


class _SdkBotError(Exception):
    """The SDK's error shape: ``code`` / ``message`` and a ``[code] message`` str."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f'[{code}] {message}')
        self.code = code
        self.message = message


class _SdkBot:
    def __init__(self, *, fail: Exception) -> None:
        self._fail = fail
        self.traceparent = None

    async def __aenter__(self) -> _SdkBot:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_status(self) -> dict:
        raise self._fail


def _fake_sdk(fail: Exception) -> types.ModuleType:
    """A ``weixin_bot`` module whose bot raises ``fail`` on ``get_status``."""
    module = types.ModuleType('weixin_bot')
    module.WeChatBot = lambda **_kwargs: _SdkBot(fail=fail)
    module.WeChatBotError = _SdkBotError
    return module


class DaemonErrorBoundaryTest(unittest.TestCase):
    """An SDK RPC failure is normalized; a programming error is not."""

    def test_an_sdk_rpc_error_becomes_the_local_unavailable_error(self) -> None:
        sdk = _fake_sdk(_SdkBotError(-13, 'session expired, need relogin'))

        with (
            patch.dict(sys.modules, {'weixin_bot': sdk}),
            self.assertRaises(WeixinUnavailableError) as caught,
        ):
            asyncio.run(WeixinSource(auto_login=True).__aenter__())

        self.assertIn('[-13] session expired', str(caught.exception))

    def test_a_programming_error_still_propagates(self) -> None:
        sdk = _fake_sdk(ValueError('programming bug'))

        with patch.dict(sys.modules, {'weixin_bot': sdk}), self.assertRaises(ValueError):
            asyncio.run(WeixinSource(auto_login=True).__aenter__())


class _Client:
    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class DaemonOutageIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def test_an_sdk_rpc_error_isolates_weixin_in_the_container(self) -> None:
        from hippo.container import AppContainer

        sdk = _fake_sdk(_SdkBotError(-13, 'session expired'))
        container = AppContainer(
            storage=SimpleNamespace(),
            client=_Client(),
            image_service=None,
            downloader=None,
            weixin_source=WeixinSource(auto_login=True),
            adapters={},
        )

        with patch.dict(sys.modules, {'weixin_bot': sdk}):
            app = await container.__aenter__()

        self.assertIsNone(app.weixin_source)
        self.assertIn('session expired', app.weixin_unavailable or '')
        with self.assertRaises(SourceError):
            await app.adapters['weixin'].sync_account(biz='b', source_key='k')
        await app.__aexit__(None, None, None)


class WeixinSourceBodyDiagnosticsTest(unittest.TestCase):
    def test_preserves_per_url_daemon_diagnostics(self) -> None:
        source = WeixinSource(auto_login=False)
        source._bot = _Bot()
        urls = ['https://mp.weixin.qq.com/s/ok', 'https://mp.weixin.qq.com/s/missing']

        bodies = asyncio.run(source.fetch_bodies(urls))

        self.assertEqual([body.url for body in bodies], [urls[0]])
        self.assertIsNone(source.body_error_for(urls[0]))
        self.assertEqual(source.body_error_for(urls[1]), '6771 empty; 2594 returned no body')

    def test_matches_body_when_daemon_normalizes_the_url(self) -> None:
        source = WeixinSource(auto_login=False)
        source._bot = _NormalizingBot()
        requested = 'http://mp.weixin.qq.com/s?__biz=Mz1&sn=abc123&scene=0#rd'

        asyncio.run(source.fetch_bodies([requested]))

        body = source.body_for(requested)
        self.assertIsNotNone(body)
        self.assertEqual(body.slug, 'slug-x')


def _fetched(**overrides) -> FetchedArticle:
    fields = {'short_link': 'https://x/s/a', 'slug': 'a', 'title': 'T', 'images': ['u'], **overrides}
    return FetchedArticle.from_dict(fields)


class MissingBodyReasonTest(unittest.TestCase):
    def test_names_the_two_daemon_silences_differently(self) -> None:
        self.assertIn('no article entry', WeixinArticleSync._missing_body_reason('u1', None))

        reason = WeixinArticleSync._missing_body_reason('u2', _fetched())
        self.assertIn('empty body', reason)
        self.assertIn("title='T'", reason)
        self.assertIn('images=1', reason)


class ClassifyDaemonErrorTest(unittest.TestCase):
    def test_session_errors_are_recognized(self) -> None:
        for msg in (
            '[401] session expired（-13）：get_article_bodies 需要重新登录（login_auto / login_qr_start）',
            '[401] 未登录：get_biz_articles 需要先登录（login_qr_start / login_storage / login_official / login_auto）',
            'session expired: NewSync 被服务端拒绝 (retcode=0xfffffff3)',
        ):
            self.assertEqual(classify_daemon_error(msg), 'session', msg)

    def test_network_and_upstream_are_not_session(self) -> None:
        self.assertEqual(classify_daemon_error('0-RTT 请求失败'), 'network')
        self.assertEqual(classify_daemon_error('read timed out'), 'network')
        self.assertEqual(
            classify_daemon_error('2594 response parsed 0 articles (raw 5B written to /tmp/x)'),
            'upstream',
        )
        self.assertEqual(classify_daemon_error('响应里没有 JSON'), 'upstream')


class _RecordingQueue:
    def __init__(self) -> None:
        self.requeued: list[int] = []
        self.failed: list[int] = []
        self.errors: list[str] = []

    async def requeue(self, ids, *, error: str) -> int:
        self.requeued.extend(int(i) for i in ids)
        return len(self.requeued)

    async def mark_failed(self, ids, *, error: str, retryable: bool, max_attempts: int = 3) -> int:
        self.failed.extend(int(i) for i in ids)
        self.errors.append(error)
        return len(self.failed)


class _FailingSource:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def fetch_bodies(self, urls):
        raise self._exc


class _BatchFailingSource:
    """daemon 把整批的失败拼成一条 error 抛出。"""

    async def fetch_bodies(self, urls):
        raise RuntimeError('; '.join(f'{url}: 正文解析失败: 响应里没有 JSON' for url in urls))


class BatchFailureAttributionTest(unittest.TestCase):
    def test_each_row_records_its_own_reason(self) -> None:
        queue = _RecordingQueue()
        sync = WeixinArticleSync(
            storage=_storage_with(queue),
            source=_BatchFailingSource(),
            downloader=None,
        )
        batch = [
            {'id': 1, 'long_link': 'http://mp.weixin.qq.com/s?sn=1'},
            {'id': 2, 'long_link': 'http://mp.weixin.qq.com/s?sn=2'},
        ]

        asyncio.run(sync._process_batch(batch, SyncStats()))

        self.assertEqual(queue.failed, [1, 2])
        self.assertEqual(queue.errors, ['正文解析失败: 响应里没有 JSON'] * 2)

    def test_split_ignores_messages_that_are_not_url_prefixed(self) -> None:
        from hippo.weixin_ingest import _split_batch_error

        self.assertEqual(_split_batch_error('connection reset by peer'), {})
        self.assertEqual(
            _split_batch_error('http://a/s?sn=1: 响应里没有 JSON; http://b/s?sn=2: 解析出 0 篇'),
            {'http://a/s?sn=1': '响应里没有 JSON', 'http://b/s?sn=2': '解析出 0 篇'},
        )


def _storage_with(queue: _RecordingQueue) -> SimpleNamespace:
    return SimpleNamespace(article_queue=queue, commit=AsyncMock(), rollback=AsyncMock())


class SessionFailureBookkeepingTest(unittest.TestCase):
    def _batch(self) -> list[dict]:
        return [{'id': 7, 'long_link': 'https://mp.weixin.qq.com/s/x'}]

    def test_session_failure_requeues_batch_without_attempts(self) -> None:
        queue = _RecordingQueue()
        sync = WeixinArticleSync(
            storage=_storage_with(queue),
            source=_FailingSource(RuntimeError('[401] session expired（-13）：x 需要重新登录')),
            downloader=None,
        )
        with self.assertRaises(SessionExpiredError):
            asyncio.run(sync._process_batch(self._batch(), SyncStats()))
        self.assertEqual(queue.requeued, [7])
        self.assertEqual(queue.failed, [], '会话失效不得记成文章失败')

    def test_upstream_failure_still_records_attempt(self) -> None:
        queue = _RecordingQueue()
        sync = WeixinArticleSync(
            storage=_storage_with(queue),
            source=_FailingSource(RuntimeError('2594 response parsed 0 articles')),
            downloader=None,
        )
        asyncio.run(sync._process_batch(self._batch(), SyncStats()))
        self.assertEqual(queue.failed, [7])
        self.assertEqual(queue.requeued, [])


if __name__ == '__main__':
    unittest.main()
