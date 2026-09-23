from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from hippo.weixin_source import SessionExpiredError, WeixinSource, classify_daemon_error
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


class WeixinSourceBodyDiagnosticsTest(unittest.TestCase):
    def test_preserves_per_url_daemon_diagnostics(self) -> None:
        source = WeixinSource(auto_login=False)
        source._bot = _Bot()
        urls = ['https://mp.weixin.qq.com/s/ok', 'https://mp.weixin.qq.com/s/missing']

        bodies = asyncio.run(source.fetch_bodies(urls))

        self.assertEqual([body.url for body in bodies], [urls[0]])
        self.assertIsNone(source.body_error_for(urls[0]))
        self.assertEqual(source.body_error_for(urls[1]), '6771 empty; 2594 returned no body')


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

    def requeue(self, ids, *, error: str) -> int:
        self.requeued.extend(int(i) for i in ids)
        return len(self.requeued)

    def mark_failed(self, ids, *, error: str, retryable: bool, max_attempts: int = 3) -> int:
        self.failed.extend(int(i) for i in ids)
        return len(self.failed)


class _FailingSource:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def fetch_bodies(self, urls):
        raise self._exc


def _storage_with(queue: _RecordingQueue) -> SimpleNamespace:
    return SimpleNamespace(article_queue=queue, commit=lambda: None, rollback=lambda: None)


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
