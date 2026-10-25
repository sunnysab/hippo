"""Reference jobs reuse the body ingester without creating feed subscriptions."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hippo.sync_types import SyncStats
from hippo.wechat_links import parse_wechat_link
from hippo.weixin_daemon import FetchedArticle
from hippo.weixin_ingest import WeixinArticleSync


def body(**changes):
    values = dict(
        url='https://mp.weixin.qq.com/s/slug',
        short_link='https://mp.weixin.qq.com/s/slug',
        slug='slug',
        title='Title',
        author='',
        digest='',
        user_name='',
        nick_name='Publisher',
        publish_time=0,
        html='<p>Body</p>',
        images=[],
        raw_json='{"bizuin":"Mz1","user_name":"gh_1","nick_name":"Publisher"}',
    )
    values.update(changes)
    return FetchedArticle(**values)


class ReferenceIngestTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.storage = SimpleNamespace(
            references=SimpleNamespace(complete=AsyncMock(), ensure_publisher=AsyncMock()),
            documents=SimpleNamespace(save=AsyncMock()),
        )
        self.downloader = SimpleNamespace(ingest_body=AsyncMock(return_value=2))
        self.sync = WeixinArticleSync(storage=self.storage, source=SimpleNamespace(), downloader=self.downloader)
        self.row = {
            'id': 12,
            'biz': '',
            'sn': 'slug:slug',
            'long_link': 'https://mp.weixin.qq.com/s/slug',
            'payload': {'source': 'article_reference'},
        }

    async def test_a_short_link_resolves_its_publisher_and_binds_the_original_queue(self):
        await self.sync._store(self.row, body())
        article = self.downloader.ingest_body.await_args.kwargs['article']
        self.assertEqual(('Mz1', 'slug'), (article.biz, article.article_id))
        self.assertIsNone(article.raw['sn'])
        self.storage.references.complete.assert_awaited_once_with(12, 2)
        self.storage.references.ensure_publisher.assert_awaited_once()
        self.assertFalse(self.downloader.ingest_body.await_args.kwargs['with_images'])

    async def test_a_long_link_keeps_its_real_sn(self):
        self.row.update(biz='Mz1', sn='abc', long_link='https://mp.weixin.qq.com/s?__biz=Mz1&sn=abc')
        await self.sync._store(self.row, body())
        self.assertEqual('abc', self.downloader.ingest_body.await_args.kwargs['article'].raw['sn'])

    async def test_unknown_or_mismatched_publishers_do_not_publish_an_article(self):
        with self.assertRaisesRegex(RuntimeError, 'no publisher'):
            await self.sync._store(self.row, body(raw_json='{}'))
        self.row['biz'] = 'MzOther'
        with self.assertRaisesRegex(RuntimeError, 'publisher does not match'):
            await self.sync._store(self.row, body())
        self.downloader.ingest_body.assert_not_awaited()
        self.storage.references.complete.assert_not_awaited()

    async def test_a_mismatched_slug_or_non_wechat_canonical_url_is_rejected(self):
        for fetched in (
            body(slug='other', short_link='https://mp.weixin.qq.com/s/other'),
            body(short_link='https://evil.test/s/slug'),
        ):
            with self.assertRaises(RuntimeError):
                await self.sync._store(self.row, fetched)
        self.downloader.ingest_body.assert_not_awaited()
        self.storage.references.complete.assert_not_awaited()

    async def test_a_completed_queued_identity_is_reused_without_fetching_again(self):
        identity = parse_wechat_link(self.row['long_link']).identity
        self.storage.references.resolve = AsyncMock(return_value={identity: {'id': 2, 'ready': True}})
        self.storage.article_queue = SimpleNamespace(mark_done=AsyncMock())
        self.storage.commit = AsyncMock()
        self.sync._source.fetch_bodies = AsyncMock()
        stats = SyncStats()
        await self.sync._process_batch([self.row], stats)
        self.sync._source.fetch_bodies.assert_not_awaited()
        self.storage.references.complete.assert_awaited_once_with(12, 2)
        self.storage.article_queue.mark_done.assert_awaited_once_with([12])
        self.assertEqual(1, stats.ingested)

    async def test_an_ingest_failure_cannot_grant_reference_access(self):
        self.downloader.ingest_body.side_effect = RuntimeError('parse failed')
        with self.assertRaisesRegex(RuntimeError, 'parse failed'):
            await self.sync._store(self.row, body())
        self.storage.references.complete.assert_not_awaited()
