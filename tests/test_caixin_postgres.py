"""Opt-in checks against an isolated PostgreSQL server, never deployment config."""

import asyncio
import json
import os
import re
import unittest
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from hippo.downloader import ArticleDownloader
from hippo.models import ArticleRecord
from hippo.sources.caixin import CaixinRateLimited
from hippo.sources.caixin_pacing import STATE_KEY, CaixinPacer, CaixinPaused
from hippo.sources.caixin_sync import CaixinSync, caixin_article_access, ingest_caixin_body
from hippo.storage import PostgresStorage

DSN = os.environ.get('HIPPO_TEST_PG_DSN')


@unittest.skipUnless(DSN, 'HIPPO_TEST_PG_DSN must point at an isolated PostgreSQL test server')
class CaixinPostgresTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = await psycopg.AsyncConnection.connect(DSN, autocommit=True)
        self.schema = f'hippo_caixin_test_{uuid.uuid4().hex}'
        await self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.cleanup)
        await self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        baseline = (Path(__file__).resolve().parent.parent / 'schema/postgres.sql').read_text()
        for table in (
            'meta',
            'account_groups',
            'accounts',
            'articles',
            'article_content',
            'article_images',
            'article_download_attempts',
            'article_document',
            'users',
            'subscription',
        ):
            ddl = re.search(rf'CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);', baseline, re.DOTALL)
            self.assertIsNotNone(ddl, table)
            await self.conn.execute(ddl.group())
        await self.conn.execute("ALTER TABLE accounts ADD COLUMN source text NOT NULL DEFAULT 'weixin'")
        await self.conn.execute('ALTER TABLE articles ADD COLUMN duration_seconds integer')
        self.storage = PostgresStorage(DSN)
        self.storage.conn = self.conn
        self.storage._bind_repositories()

    async def cleanup(self):
        await self.conn.rollback()
        await self.conn.set_autocommit(True)
        await self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        await self.conn.close()

    @asynccontextmanager
    async def open_independent_storage(self):
        async with await psycopg.AsyncConnection.connect(DSN, options=f'-csearch_path={self.schema}') as conn:
            storage = PostgresStorage(DSN)
            storage.conn = conn
            storage._bind_repositories()
            yield storage

    async def test_independent_connections_serialize_and_preserve_retry_after(self):
        gate = asyncio.Event()

        async def contender():
            await gate.wait()
            try:
                async with CaixinPacer().request('article'):
                    await asyncio.sleep(0)
                    return 'entered'
            except CaixinPaused:
                return 'paused'

        with patch('hippo.sources.caixin_pacing._open_storage', self.open_independent_storage):
            tasks = [asyncio.create_task(contender()) for _ in range(2)]
            gate.set()
            self.assertCountEqual(['entered', 'paused'], await asyncio.gather(*tasks))
            state = json.loads(await self.storage.meta.get(STATE_KEY))
            self.assertEqual(1, state['daily_ops'])
            self.assertEqual(1, state['daily_articles'])
            self.assertEqual(0, state['lease_until'])
            with self.assertRaises(CaixinPaused):
                async with CaixinPacer().request('list'):
                    self.fail('a new caller reset the cooldown')
            state['next_at'] = 0
            await self.storage.meta.set(STATE_KEY, json.dumps(state))
            with self.assertRaises(CaixinRateLimited):
                async with CaixinPacer().request('article'):
                    raise CaixinRateLimited(7200)
            with self.assertRaises(CaixinPaused) as caught:
                async with CaixinPacer().request('list'):
                    self.fail('Retry-After was lost')
            self.assertGreaterEqual(caught.exception.retry_after_seconds, 7199)
            state = json.loads(await self.storage.meta.get(STATE_KEY))
            self.assertEqual(2, state['daily_articles'])

    async def test_workers_and_manual_refetch_share_identity_ownership_until_commit(self):
        await self.conn.execute("""
            INSERT INTO accounts (biz, nickname, source, created_at, updated_at)
            VALUES ('caixin:132', 'World', 'caixin', NOW(), NOW()),
                   ('caixin:113', 'Finance', 'caixin', NOW(), NOW());
            INSERT INTO articles (id, biz, article_id, title, link, raw_json, created_at, updated_at)
            VALUES (1, 'caixin:132', '1001', 'Title', 'https://example.test', '{}', NOW(), NOW()),
                   (2, 'caixin:113', '1001', 'Title', 'https://example.test', '{}', NOW(), NOW());
            SELECT setval(pg_get_serial_sequence('articles', 'id'), 2);
        """)
        source = SimpleNamespace(fetch_article=AsyncMock(return_value={'content_html': '<p>Complete body</p>'}))
        source.with_credential = Mock(return_value=source)
        reached, resume = asyncio.Event(), asyncio.Event()
        async with self.open_independent_storage() as first, self.open_independent_storage() as second:
            adapters = []
            for pk, storage in ((1, first), (2, second)):
                cursor = await storage.conn.execute('SELECT * FROM articles WHERE id=%s', (pk,))
                cursor.row_factory = dict_row
                row = await cursor.fetchone()
                await storage.commit()
                storage.articles.list_missing_bodies = AsyncMock(return_value=[row])
                downloader = ArticleDownloader(client=SimpleNamespace(), storage=storage, enable_image_worker=False)
                if pk == 1:
                    ingest = downloader.ingest_body

                    async def delayed_ingest(_ingest=ingest, **kwargs):
                        reached.set()
                        await resume.wait()
                        return await _ingest(**kwargs)

                    downloader.ingest_body = delayed_ingest
                adapters.append(
                    CaixinSync(
                        storage=storage,
                        source=source,
                        credentials=AsyncMock(return_value=('',)),
                        downloader=downloader,
                    )
                )
            task = asyncio.create_task(adapters[0].drain(limit=1))
            try:
                await asyncio.wait_for(reached.wait(), timeout=5)
                self.assertEqual(0, (await adapters[1].drain(limit=1)).ingested)
                with self.assertRaises(CaixinPaused):
                    async with caixin_article_access(second, '1001'):
                        self.fail('manual refetch bypassed worker ownership')
                source.fetch_article.assert_awaited_once()
            finally:
                resume.set()
                await task
            self.assertEqual(1, task.result().ingested)
            self.assertEqual(1, (await adapters[1].drain(limit=1)).ingested)
            source.fetch_article.assert_awaited_once()
            self.assertEqual('<p>Complete body</p>', (await second.documents.get(2))['raw_html'])
            await second.commit()
            with self.assertRaises(asyncio.CancelledError):
                async with caixin_article_access(first, '1001'):
                    raise asyncio.CancelledError
            async with caixin_article_access(second, '1001'):
                pass

    async def test_backlog_filters_and_cross_channel_document_reuse(self):
        await self.conn.execute("""
            INSERT INTO accounts (biz, nickname, source, created_at, updated_at)
            VALUES ('caixin:132', 'World', 'caixin', NOW(), NOW()),
                   ('caixin:113', 'Finance', 'caixin', NOW(), NOW()),
                   ('caixin:disabled', 'Disabled', 'caixin', NOW(), NOW()),
                   ('caixin:unsubscribed', 'Unsubscribed', 'caixin', NOW(), NOW());
            UPDATE accounts SET is_disabled=TRUE WHERE biz='caixin:disabled';
            INSERT INTO users (id, username, password_hash, created_at, updated_at)
            VALUES (7, 'reader', 'test-only', NOW(), NOW());
            INSERT INTO subscription (user_id, biz, created_at)
            SELECT 7, biz, NOW() FROM accounts WHERE biz <> 'caixin:unsubscribed';
        """)
        for pk, biz, identity in (
            (1, 'caixin:132', '1001'),
            (2, 'caixin:113', '1001'),
            (3, 'caixin:132', '1003'),
            (4, 'caixin:132', '1004'),
            (5, 'caixin:disabled', '1005'),
            (6, 'caixin:unsubscribed', '1006'),
        ):
            await self.conn.execute(
                """INSERT INTO articles (id, biz, article_id, title, link, raw_json, created_at, updated_at)
                   VALUES (%s, %s, %s, 'Title', 'https://example.test', '{}', NOW(), NOW())""",
                (pk, biz, identity),
            )
        await self.conn.execute("""
            INSERT INTO article_content (article_pk, content_markdown, created_at, updated_at)
            VALUES (1, 'Complete body', NOW(), NOW());
            INSERT INTO article_download_attempts (biz, article_id, attempts, retryable, created_at)
            VALUES ('caixin:132', '1003', 3, TRUE, NOW()), ('caixin:132', '1004', 1, FALSE, NOW());
        """)
        await self.storage.documents.save(
            article_pk=1,
            source='caixin',
            raw_html='<p>Complete body</p>',
            raw_json={'article_id': 1001},
        )
        rows = await self.storage.articles.list_missing_bodies(source='caixin', limit=5)
        self.assertEqual([2], [row['id'] for row in rows])
        cached = await self.storage.documents.find_by_identity(source='caixin', article_id='1001')
        self.assertEqual('<p>Complete body</p>', cached['raw_html'])
        self.assertEqual(1001, cached['raw_json']['article_id'])
        self.assertIsNone(await self.storage.documents.find_by_identity(source='other', article_id='1001'))

        # Verify the real downloader's internal commit cannot leave a body without its raw archive.
        await self.conn.execute("SELECT setval(pg_get_serial_sequence('articles', 'id'), 6)")
        await self.conn.set_autocommit(False)
        downloader = ArticleDownloader(client=SimpleNamespace(), storage=self.storage, enable_image_worker=False)
        article = ArticleRecord(
            biz='caixin:113',
            article_id='1001',
            title='Title',
            link='https://example.test',
            author=None,
            digest=None,
            cover=None,
            source_url=None,
            publish_at=None,
            raw={},
        )
        body = {'article_id': 1001, 'content_html': '<p>Complete body</p>'}
        with (
            patch.object(self.storage.documents, 'save', side_effect=RuntimeError('archive unavailable')),
            self.assertRaises(RuntimeError),
        ):
            await ingest_caixin_body(
                storage=self.storage,
                downloader=downloader,
                article=article,
                article_pk=2,
                body=body,
            )
        self.assertFalse(await self.storage.articles.has_article_content('caixin:113', '1001'))
        await self.conn.commit()
        save_content = self.storage.articles.save_article_content

        async def interrupted_save(*args, **kwargs):
            await save_content(*args, **kwargs)
            raise RuntimeError('content write interrupted')

        with (
            patch.object(self.storage.articles, 'save_article_content', interrupted_save),
            self.assertRaises(RuntimeError),
        ):
            await ingest_caixin_body(
                storage=self.storage,
                downloader=downloader,
                article=article,
                article_pk=2,
                body=body,
            )
        self.assertIsNone(await self.storage.documents.get(2))
        self.assertFalse(await self.storage.articles.has_article_content('caixin:113', '1001'))
        await self.conn.commit()
        await ingest_caixin_body(
            storage=self.storage,
            downloader=downloader,
            article=article,
            article_pk=2,
            body=body,
        )
        async with self.open_independent_storage() as observer:
            self.assertTrue(await observer.articles.has_article_content('caixin:113', '1001'))
            self.assertEqual(body['content_html'], (await observer.documents.get(2))['raw_html'])
        await self.conn.rollback()
        await self.conn.set_autocommit(True)
