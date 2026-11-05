"""Opt-in PostgreSQL checks; use only an isolated test server via HIPPO_TEST_PG_DSN.

Each case owns a unique schema and removes only that schema. The production
configuration is never read and the full historical migration is never executed.
"""

import asyncio
import json
import os
import re
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from hippo.article_queries import _assert_article_visible, _ensure_image_visible, _get_article
from hippo.article_references import article_references
from hippo.downloader import ArticleDownloader
from hippo.exceptions import ApiError
from hippo.repositories.article import ArticleRepository
from hippo.repositories.reference import ArticleReferenceRepository
from hippo.storage import PostgresStorage
from hippo.wechat_links import parse_wechat_link
from hippo.weixin_daemon import FetchedArticle
from hippo.weixin_ingest import WeixinArticleSync

DSN = os.environ.get('HIPPO_TEST_PG_DSN')
LONG = 'https://mp.weixin.qq.com/s?__biz=MzForeign&mid=123&idx=2&sn=abc'


@unittest.skipUnless(DSN, 'HIPPO_TEST_PG_DSN must point at an isolated PostgreSQL test server')
class ReferencePostgresTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = await psycopg.AsyncConnection.connect(DSN, autocommit=True)
        self.schema = f'hippo_reference_test_{uuid.uuid4().hex}'
        await self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.cleanup)
        await self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        schema = (Path(__file__).resolve().parent.parent / 'schema/postgres.sql').read_text()
        for table in (
            'account_groups',
            'accounts',
            'articles',
            'article_content',
            'article_queue',
            'users',
            # ``_get_article`` reads the caller's Watch Later badge, so the side
            # table has to exist before the detail route is exercised.
            'article_watch_later',
            'subscription',
            'article_images',
            'blocked_image_hashes',
            'article_read',
            'article_star',
            'article_document',
        ):
            ddl = re.search(rf'CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);', schema, re.DOTALL)
            self.assertIsNotNone(ddl, table)
            await self.conn.execute(ddl.group())
        await self.conn.execute("ALTER TABLE accounts ADD COLUMN source text NOT NULL DEFAULT 'weixin'")
        await self.conn.execute('ALTER TABLE articles ADD COLUMN duration_seconds integer')
        migration = schema[schema.index('-- Direct references grant access') :]
        await self.conn.execute(migration)
        await self.conn.execute(migration)
        await self.conn.execute("""
            INSERT INTO accounts (biz, nickname, created_at, updated_at)
            VALUES ('MzRoot', 'Root', NOW(), NOW()), ('MzForeign', 'Foreign', NOW(), NOW());
            INSERT INTO users (id, username, password_hash, created_at, updated_at)
            VALUES (7, 'reader', 'test-only', NOW(), NOW()), (8, 'other', 'test-only', NOW(), NOW());
            INSERT INTO subscription (user_id, biz, created_at) VALUES (7, 'MzRoot', NOW());
        """)
        for pk, biz, slug, sn in (
            (1, 'MzRoot', 'root', 'root-sn'),
            (2, 'MzForeign', 'target', 'abc'),
            (3, 'MzForeign', 'other', 'other-sn'),
            (4, 'MzForeign', 'child', 'child-sn'),
            (5, 'MzRoot', 'root-two', 'root-two-sn'),
        ):
            await self.conn.execute(
                """INSERT INTO articles (id, biz, article_id, title, link, raw_json, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())""",
                (pk, biz, slug, slug, f'https://mp.weixin.qq.com/s/{slug}', json.dumps({'sn': sn})),
            )
            text = f'**[Target]({LONG})**' if pk in {1, 5} else 'Body'
            await self.conn.execute(
                """INSERT INTO article_content (article_pk, content_json, content_markdown, created_at, updated_at)
                   VALUES (%s, %s, %s, NOW(), NOW())""",
                (pk, Jsonb([{'type': 'paragraph', 'text': text}]), text),
            )
        await self.conn.execute("""
            INSERT INTO article_images (id, article_pk, position, kind, updated_at)
            VALUES (9, 2, 0, 'inline', NOW()), (10, 3, 0, 'inline', NOW())
        """)
        await self.conn.execute("SELECT setval(pg_get_serial_sequence('articles', 'id'), 5)")
        await self.conn.set_autocommit(False)
        self.storage = PostgresStorage(DSN)
        self.storage.conn = self.conn
        self.storage._bind_repositories()
        # Topic classification and event clustering are unrelated enrichment this
        # fixture never creates tables for. ``_attach_topics`` and the event
        # lookup both guard on the repository, so leaving it out keeps the fixture
        # on the reference tables under test instead of failing with
        # UndefinedTable partway through the detail route.
        self.storage.topics = None

    async def cleanup(self):
        await self.conn.rollback()
        await self.conn.set_autocommit(True)
        await self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        await self.conn.close()

    async def denied(self, article_id, *, user_id=7):
        with self.assertRaises(ApiError) as raised:
            await _assert_article_visible(self.storage, user_id, article_id)
        self.assertEqual(404, raised.exception.status)

    async def test_lookup_is_read_only_and_post_grants_only_the_direct_article_and_images(self):
        result = await article_references(self.storage, 7, 1)
        self.assertEqual(('ready', 2), (result['items'][0]['status'], result['items'][0]['article_id']))
        self.assertEqual(0, (await (await self.conn.execute('SELECT count(*) FROM article_reference')).fetchone())[0])
        await self.denied(2)
        await article_references(self.storage, 7, 1, enqueue=True)
        await _assert_article_visible(self.storage, 7, 2)
        await _ensure_image_visible(self.storage, 7, 9)
        await self.denied(3)
        await self.denied(4)
        await self.denied(2, user_id=8)
        with self.assertRaises(ApiError):
            await _ensure_image_visible(self.storage, 7, 10)
        self.assertEqual(0, (await (await self.conn.execute('SELECT count(*) FROM article_queue')).fetchone())[0])
        self.assertEqual({'items': []}, await article_references(self.storage, 7, 2, enqueue=True))

    async def test_missing_short_reference_is_ingested_and_readable_without_subscribing(self):
        url = 'https://mp.weixin.qq.com/s/brand-new'
        await self.conn.execute(
            'UPDATE article_content SET content_json=%s WHERE article_pk=1',
            (Jsonb([{'type': 'paragraph', 'text': f'**[New article]({url})**'}]),),
        )
        async with self.storage.transaction():
            result = await article_references(self.storage, 7, 1, enqueue=True)
        self.assertEqual('pending', result['items'][0]['status'])
        fetched = FetchedArticle(
            url=url,
            short_link=url,
            slug='brand-new',
            title='New article',
            author='',
            digest='',
            user_name='gh_new',
            nick_name='New publisher',
            publish_time=0,
            html='<h1>New article</h1><p>Fetched body</p><img src="https://cdn.test/image.png">',
            images=['https://cdn.test/image.png'],
            raw_json='{"bizuin":"MzNew","user_name":"gh_new","nick_name":"New publisher"}',
        )
        source = SimpleNamespace(fetch_bodies=AsyncMock(), body_for=lambda value: fetched)
        downloader = ArticleDownloader(client=SimpleNamespace(), storage=self.storage)
        sync = WeixinArticleSync(storage=self.storage, source=source, downloader=downloader)
        stats = await sync.drain(limit=1)
        self.assertEqual((1, 0), (stats.ingested, stats.failed))
        result = await article_references(self.storage, 7, 1)
        self.assertEqual('ready', result['items'][0]['status'])
        target = result['items'][0]['article_id']
        self.assertIsNotNone(target)
        detail = await _get_article(self.storage, 7, target)
        self.assertEqual('ok', detail['content_status'])
        self.assertEqual('MzNew', detail['article']['biz'])
        self.assertEqual('Fetched body', detail['content'][0]['text'])
        image = detail['images'][0]['id']
        await _ensure_image_visible(self.storage, 7, image)
        self.assertEqual(1, (await (await self.conn.execute('SELECT count(*) FROM subscription')).fetchone())[0])
        async with self.storage.transaction():
            await article_references(self.storage, 7, 1, enqueue=True)
        self.assertEqual(0, (await sync.drain(limit=1)).ingested)
        source.fetch_bodies.assert_awaited_once_with([url])

    async def test_source_revision_and_subscription_changes_revoke_access(self):
        await article_references(self.storage, 7, 1, enqueue=True)
        await self.conn.execute(
            "UPDATE article_content SET updated_at = updated_at + interval '1 second' WHERE article_pk=1"
        )
        await self.denied(2)
        await article_references(self.storage, 7, 1, enqueue=True)
        await _assert_article_visible(self.storage, 7, 2)
        await self.conn.execute('UPDATE subscription SET is_disabled=TRUE WHERE user_id=7')
        await self.denied(2)
        await self.conn.execute('UPDATE subscription SET is_disabled=FALSE WHERE user_id=7')
        await _assert_article_visible(self.storage, 7, 2)
        await self.conn.execute('DELETE FROM subscription WHERE user_id=7')
        await self.denied(2)

    async def test_removing_a_link_deletes_its_grant_on_refresh(self):
        await article_references(self.storage, 7, 1, enqueue=True)
        await self.conn.execute(
            "UPDATE article_content SET content_json=%s, updated_at=updated_at+interval '1 second' WHERE article_pk=1",
            (Jsonb([{'type': 'paragraph', 'text': 'No links'}]),),
        )
        self.assertEqual({'items': []}, await article_references(self.storage, 7, 1, enqueue=True))
        await self.denied(2)
        self.assertEqual(0, (await (await self.conn.execute('SELECT count(*) FROM article_reference')).fetchone())[0])

    async def test_a_stale_lookup_cannot_erase_a_completed_reference_binding(self):
        link = parse_wechat_link(LONG)
        await self.storage.references.register(1, [link], {link.identity: {'id': 2}})
        await self.storage.references.register(1, [link], {})
        await _assert_article_visible(self.storage, 7, 2)

    async def test_concurrent_sources_can_bind_the_same_queue_without_cross_source_deadlock(self):
        link = parse_wechat_link(LONG)
        await self.storage.references.register(1, [link], {})
        async with await psycopg.AsyncConnection.connect(DSN) as other:
            await other.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
            repository = ArticleReferenceRepository(other)
            await repository.register(5, [link], {})

            async def enqueue_and_commit(repo, conn, source):
                queue_id = await repo.enqueue(link, article_pk=source)
                await conn.commit()
                return queue_id

            results = await asyncio.wait_for(
                asyncio.gather(
                    enqueue_and_commit(self.storage.references, self.conn, 1),
                    enqueue_and_commit(repository, other, 5),
                ),
                timeout=10,
            )
            self.assertEqual(results[0], results[1])
        rows = await (
            await self.conn.execute(
                'SELECT source_article_pk, queue_id FROM article_reference ORDER BY source_article_pk',
            )
        ).fetchall()
        self.assertEqual([(1, results[0]), (5, results[0])], rows)

    async def test_long_share_urls_do_not_become_oversized_btree_keys(self):
        link = parse_wechat_link(LONG + '&tracking=' + 'x' * 4000)
        await self.storage.references.register(1, [link], {link.identity: {'id': 2}})
        await _assert_article_visible(self.storage, 7, 2)
        result = await self.storage.references.resolve([link])
        self.assertEqual(2, result[link.identity]['id'])

    async def test_reopening_does_not_reset_a_processing_or_failed_queue_item(self):
        link = parse_wechat_link('https://mp.weixin.qq.com/s/not-yet-fetched')
        queue_id = await self.storage.references.enqueue(link, article_pk=1)
        for state in ('processing', 'failed'):
            await self.conn.execute('UPDATE article_queue SET state=%s, attempts=3 WHERE id=%s', (state, queue_id))
            self.assertEqual(queue_id, await self.storage.references.enqueue(link, article_pk=1))
            row = await (
                await self.conn.execute('SELECT state, attempts FROM article_queue WHERE id=%s', (queue_id,))
            ).fetchone()
            self.assertEqual((state, 3), row)
        await self.storage.references.enqueue(link, article_pk=1, retry=True)
        row = await (
            await self.conn.execute('SELECT state, attempts FROM article_queue WHERE id=%s', (queue_id,))
        ).fetchone()
        self.assertEqual(('pending', 0), row)
        self.assertEqual('pending', (await self.storage.references.states([link]))[link.identity]['state'])

    async def test_completion_resolves_all_sources_and_reuses_the_short_link(self):
        link = parse_wechat_link('https://mp.weixin.qq.com/s?__biz=MzForeign&mid=123&idx=2')
        for source in (1, 5):
            await self.storage.references.register(source, [link], {})
        queue_id = await self.storage.references.enqueue(link, article_pk=1)
        await self.storage.references.enqueue(link, article_pk=5)
        await self.storage.references.complete(queue_id, 2)
        result = await self.storage.references.resolve([link, parse_wechat_link('https://mp.weixin.qq.com/s/target')])
        self.assertEqual({2}, {row['id'] for row in result.values()})
        self.assertEqual(2, len(result))
        count = await (
            await self.conn.execute('SELECT count(*) FROM article_reference WHERE target_article_pk=2')
        ).fetchone()
        self.assertEqual(2, count[0])

    async def test_empty_or_malformed_blocks_are_not_ready_and_metadata_refetch_keeps_sn(self):
        for blocks in ([], {}, None):
            await self.conn.execute('UPDATE article_content SET content_json=%s WHERE article_pk=2', (Jsonb(blocks),))
            result = await self.storage.references.resolve([parse_wechat_link(LONG)])
            self.assertFalse(result[parse_wechat_link(LONG).identity]['ready'])
        async with self.conn.cursor() as cursor:
            await ArticleRepository._upsert_article_row(
                cursor,
                biz='MzForeign',
                article_id='target',
                title='Refetched',
                item_show_type=None,
                author=None,
                digest=None,
                link='https://mp.weixin.qq.com/s/target',
                source_url=None,
                publish_at=None,
                raw_json='{}',
                now='2026-10-25T00:00:00Z',
            )
        row = await (await self.conn.execute('SELECT raw_json FROM articles WHERE id=2')).fetchone()
        self.assertEqual('abc', json.loads(row[0])['sn'])
