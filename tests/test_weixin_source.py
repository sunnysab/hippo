from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hippo.weixin_source import FetchedArticle, SessionExpiredError, WeixinSource, classify_daemon_error
from hippo.weixin_worker import SyncStats, WeixinArticleSync


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
        source._bot = _ListBot([
            {
                'title': 'a\x00b',
                'digest': 'x\x00',
                'url': 'https://mp.weixin.qq.com/s?__biz=Mz1&sn=abc',
                'publish_time': 1,
                'cover_url': 'https://example.invalid/c\x00.png',
            }
        ])

        listed = asyncio.run(source.list_articles('gh_1', 'Mz1'))

        self.assertEqual(1, len(listed.items))
        payload = listed.items[0].payload
        self.assertEqual('ab', payload['title'])
        self.assertEqual('x', payload['digest'])
        self.assertEqual('https://example.invalid/c.png', payload['cover_url'])
        self.assertEqual('cur', listed.next_offset)


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
        from hippo.weixin_worker import _split_batch_error

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
