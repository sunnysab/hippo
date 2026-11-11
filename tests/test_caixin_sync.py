"""Caixin discovers cheaply and commits each missing body independently."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from hippo.sources import SourceError
from hippo.sources.caixin_pacing import CaixinPaused
from hippo.sources.caixin_sync import CaixinSync
from hippo.sources.ingest import Item, Listing
from hippo.sync_types import SyncStats

BIZ = 'caixin:132'
ARTICLE_ID = '102500001'
URL = f'https://international.caixin.com/2026-11-11/{ARTICLE_ID}.html'
BODY = {
    'article_id': int(ARTICLE_ID),
    'title': 'Fetched title',
    'content_html': '<p>The complete body.</p>',
    'author': 'Fetched author',
}


class CaixinBodyWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_weixin_queue_still_drains_caixin_without_opening_weixin(self):
        from hippo.sync_worker import drain_bodies_once

        storage = SimpleNamespace(article_queue=SimpleNamespace(stats=AsyncMock(return_value={})))
        adapter = SimpleNamespace(drain=AsyncMock(return_value=SyncStats(ingested=1)))
        container = AsyncMock()
        container.__aenter__.return_value = SimpleNamespace(adapters={'caixin': adapter})
        with (
            patch('hippo.sync_worker.get_sync_settings', AsyncMock(return_value={'download_content': True})),
            patch('hippo.sync_worker.get_media_settings', AsyncMock(return_value={})),
            patch('hippo.sync_worker.build_sync_container', return_value=container) as build,
        ):
            self.assertEqual(1, await drain_bodies_once(storage, limit=2))
        self.assertEqual({'caixin'}, build.call_args.kwargs['sources'])
        adapter.drain.assert_awaited_once_with(limit=2)


def row(article_id=ARTICLE_ID, *, attempts=0):
    return {
        'id': int(article_id),
        'biz': BIZ,
        'article_id': article_id,
        'title': 'Stored title',
        'link': f'https://international.caixin.com/2026-11-11/{article_id}.html',
        'source_url': f'https://international.caixin.com/2026-11-11/{article_id}.html',
        'author': 'Stored author',
        'digest': 'Stored summary',
        'cover': None,
        'cover_url': None,
        'publish_at': 1794398400,
        'raw_json': json.dumps({'channel_id': 132, 'is_paid': True}),
        'attempts': attempts,
    }


class _Transaction:
    def __init__(self, storage):
        self.storage = storage

    async def __aenter__(self):
        self.storage.depth += 1
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.storage.events.append('commit' if exc_type is None else 'rollback')
        self.storage.depth -= 1
        return False


class CaixinSyncTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.storage = SimpleNamespace(
            conn=SimpleNamespace(
                execute=AsyncMock(return_value=SimpleNamespace(fetchone=AsyncMock(return_value=(True,))))
            ),
            articles=SimpleNamespace(
                save_articles=AsyncMock(return_value=1),
                list_missing_bodies=AsyncMock(return_value=[row()]),
                has_article_content=AsyncMock(return_value=False),
            ),
            documents=SimpleNamespace(
                find_by_identity=AsyncMock(return_value=None),
                save=AsyncMock(),
            ),
            accounts=SimpleNamespace(
                get_account=AsyncMock(return_value=SimpleNamespace(is_disabled=False)),
            ),
            download_attempts=SimpleNamespace(increment_attempt=AsyncMock()),
            commit=AsyncMock(),
            rollback=AsyncMock(),
            depth=0,
            events=[],
        )
        self.storage.transaction = lambda: _Transaction(self.storage)
        self.source = SimpleNamespace(
            source='caixin',
            list_items=AsyncMock(
                return_value=Listing(
                    items=[
                        Item(
                            article_id=ARTICLE_ID,
                            title='Listed title',
                            link=URL,
                            media_url='',
                            publish_at=1794398400,
                            digest='Listed summary',
                            author='Listed author',
                            raw={'channel_id': 132, 'is_paid': True},
                        )
                    ],
                    is_end=True,
                )
            ),
            fetch_article=AsyncMock(return_value=BODY),
        )
        self.source.with_credential = Mock(return_value=self.source)
        self.credentials = AsyncMock(return_value=('first-cookie', 'second-cookie'))

        async def ingest_body(**kwargs):
            self.assertGreater(self.storage.depth, 0, 'body must be written in a transaction')
            self.storage.events.append('body')
            return 42

        async def save_document(**kwargs):
            self.assertGreater(self.storage.depth, 0, 'raw document must share the body transaction')
            self.storage.events.append('document')

        self.downloader = SimpleNamespace(ingest_body=AsyncMock(side_effect=ingest_body))
        self.storage.documents.save.side_effect = save_document
        self.adapter = CaixinSync(
            storage=self.storage,
            source=self.source,
            credentials=self.credentials,
            downloader=self.downloader,
        )
        settings = patch(
            'hippo.sources.caixin_sync.get_media_settings',
            AsyncMock(return_value={'items_per_sync': 5}),
        )
        settings.start()
        self.addCleanup(settings.stop)

    async def test_listing_stores_metadata_without_fetching_bodies(self):
        stats = await self.adapter.sync_account(biz=BIZ, source_key='132')

        self.assertEqual(1, stats.listed)
        self.assertEqual(1, stats.enqueued)
        self.source.list_items.assert_awaited_once_with(source_key='132', limit=5, cursor=None)
        self.source.fetch_article.assert_not_awaited()
        self.downloader.ingest_body.assert_not_awaited()
        (record,) = self.storage.articles.save_articles.call_args.args[0]
        self.assertEqual(BIZ, record.biz)
        self.assertEqual(ARTICLE_ID, record.article_id)
        self.assertEqual('Listed title', record.title)
        self.assertEqual('Listed summary', record.digest)
        self.assertEqual('Listed author', record.author)
        self.assertEqual(URL, record.link)
        self.assertEqual(1794398400, record.publish_at)
        self.assertEqual({'channel_id': 132, 'is_paid': True}, record.raw)

    async def test_repeated_listing_counts_insertions_instead_of_returned_rows(self):
        self.storage.articles.save_articles.side_effect = [1, 0]
        first = await self.adapter.sync_account(biz=BIZ, source_key='132')
        repeated = await self.adapter.sync_account(biz=BIZ, source_key='132')

        self.assertEqual((1, 1), (first.listed, repeated.listed))
        self.assertEqual((1, 0), (first.enqueued, repeated.enqueued))
        self.source.fetch_article.assert_not_awaited()

    async def test_older_page_starts_after_the_latest_batch(self):
        await self.adapter.sync_older(biz=BIZ, source_key='132', cursor=None)

        self.source.list_items.assert_awaited_once_with(source_key='132', limit=5, cursor='5')

    async def test_older_page_resumes_from_the_saved_cursor(self):
        await self.adapter.sync_older(biz=BIZ, source_key='132', cursor='15')

        self.source.list_items.assert_awaited_once_with(source_key='132', limit=5, cursor='15')

    async def test_body_and_raw_document_commit_together_and_keep_article_identity(self):
        stats = await self.adapter.drain(limit=1)

        self.assertEqual(1, stats.ingested)
        self.assertEqual(0, stats.failed)
        self.source.with_credential.assert_called_once_with('first-cookie')
        self.source.fetch_article.assert_awaited_once_with(URL)
        call = self.downloader.ingest_body.call_args.kwargs
        record = call['article']
        self.assertEqual((BIZ, ARTICLE_ID, URL), (record.biz, record.article_id, record.link))
        self.assertEqual('Stored summary', record.digest)
        self.assertEqual('Stored author', record.author)
        self.assertEqual(1794398400, record.publish_at)
        self.assertEqual({'channel_id': 132, 'is_paid': True}, record.raw)
        self.assertEqual(BODY['content_html'], call['html'])
        self.storage.documents.save.assert_awaited_once_with(
            article_pk=int(ARTICLE_ID),
            source='caixin',
            raw_html=BODY['content_html'],
            raw_json=BODY,
        )
        self.assertEqual(['document', 'body', 'commit', 'commit'], self.storage.events)

    async def test_an_existing_body_is_not_fetched_again(self):
        self.storage.articles.has_article_content.return_value = True

        await self.adapter.drain(limit=1)

        self.source.fetch_article.assert_not_awaited()
        self.credentials.assert_not_awaited()
        self.downloader.ingest_body.assert_not_awaited()

    async def test_a_channel_disabled_after_selection_stops_before_upstream(self):
        with patch(
            'hippo.sources.caixin_sync.caixin_channel_is_collectable',
            AsyncMock(return_value=False),
        ):
            stats = await self.adapter.drain(limit=1)

        self.assertEqual((0, 0), (stats.ingested, stats.failed))
        self.credentials.assert_not_awaited()
        self.source.fetch_article.assert_not_awaited()
        self.storage.download_attempts.increment_attempt.assert_not_awaited()

    async def test_an_archived_identity_reuses_its_body_without_upstream_access(self):
        self.storage.documents.find_by_identity.return_value = {
            'raw_html': BODY['content_html'],
            'raw_json': BODY,
        }

        stats = await self.adapter.drain(limit=1)

        self.assertEqual(1, stats.ingested)
        self.source.fetch_article.assert_not_awaited()
        self.credentials.assert_not_awaited()
        self.assertEqual(BODY['content_html'], self.downloader.ingest_body.call_args.kwargs['html'])

    async def test_a_later_failure_keeps_the_earlier_body_committed(self):
        second_id = '102500002'
        self.storage.articles.list_missing_bodies.return_value = [row(), row(second_id)]
        self.source.fetch_article.side_effect = [BODY, SourceError('upstream failed')]

        stats = await self.adapter.drain(limit=5)

        self.assertEqual((1, 1), (stats.ingested, stats.failed))
        self.downloader.ingest_body.assert_awaited_once()
        self.storage.documents.save.assert_awaited_once()
        self.assertEqual(['document', 'body', 'commit'], self.storage.events[:3])
        self.storage.download_attempts.increment_attempt.assert_awaited_once_with(BIZ, second_id, 'SourceError')

    async def test_resting_is_not_a_failed_download_attempt(self):
        self.source.fetch_article.side_effect = CaixinPaused(120)

        stats = await self.adapter.drain(limit=1)

        self.assertEqual((0, 0), (stats.ingested, stats.failed))
        self.storage.download_attempts.increment_attempt.assert_not_awaited()
        self.downloader.ingest_body.assert_not_awaited()

    async def test_credentials_rotate_on_a_later_attempt_instead_of_retrying_in_a_burst(self):
        self.storage.articles.list_missing_bodies.return_value = [row(attempts=1)]

        stats = await self.adapter.drain(limit=1)

        self.assertEqual(1, stats.ingested)
        self.source.with_credential.assert_called_once_with('second-cookie')
        self.source.fetch_article.assert_awaited_once()
