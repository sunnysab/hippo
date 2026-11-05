"""Opt-in Watch Later checks; use only an isolated test server via HIPPO_TEST_PG_DSN.

The unit tests read SQL text, which cannot prove what Postgres does with the
constraints, the ordering under a concurrent append, or which rows the shared
visibility join actually keeps. This file owns a throwaway schema and removes
only that schema; the production configuration is never read.
"""

import asyncio
import os
import re
import unittest
import uuid
from contextlib import suppress
from pathlib import Path

import psycopg
from psycopg import sql

from hippo.article_queries import (
    _assert_article_visible,
    _list_watch_later,
    _move_watch_later,
)
from hippo.exceptions import ApiError
from hippo.storage import PostgresStorage

DSN = os.environ.get('HIPPO_TEST_PG_DSN')

SCHEMA_PATH = Path(__file__).resolve().parent.parent / 'schema/postgres.sql'

#: Only the tables these cases touch, in dependency order. ``article_watch_later``
#: comes last: it is the table under test and hangs off both ``users`` and
#: ``articles``.
TABLES = (
    'account_groups',
    'accounts',
    'articles',
    'article_content',
    'article_images',
    'blocked_image_hashes',
    'article_queue',
    'users',
    'subscription',
    'article_read',
    'article_star',
    'article_reference',
    'article_watch_later',
)


@unittest.skipUnless(DSN, 'HIPPO_TEST_PG_DSN must point at an isolated PostgreSQL test server')
class WatchLaterPostgresTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = await psycopg.AsyncConnection.connect(DSN, autocommit=True)
        self.schema = f'hippo_watch_later_test_{uuid.uuid4().hex}'
        await self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.cleanup)
        await self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        definitions = SCHEMA_PATH.read_text(encoding='utf-8')
        for table in TABLES:
            ddl = re.search(rf'CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);', definitions, re.DOTALL)
            self.assertIsNotNone(ddl, table)
            await self.conn.execute(ddl.group())
        await self.conn.execute("ALTER TABLE accounts ADD COLUMN source text NOT NULL DEFAULT 'weixin'")
        await self.conn.execute('ALTER TABLE articles ADD COLUMN duration_seconds integer')
        await self.conn.execute("""
            INSERT INTO accounts (biz, nickname, created_at, updated_at)
            VALUES ('MzRoot', 'Root', NOW(), NOW()), ('MzForeign', 'Foreign', NOW(), NOW());
            INSERT INTO users (id, username, password_hash, created_at, updated_at)
            VALUES (7, 'reader', 'test-only', NOW(), NOW()), (8, 'other', 'test-only', NOW(), NOW());
            INSERT INTO subscription (user_id, biz, created_at) VALUES (7, 'MzRoot', NOW());
        """)
        for pk, biz in ((1, 'MzRoot'), (2, 'MzForeign'), (3, 'MzForeign')):
            await self.conn.execute(
                """INSERT INTO articles (id, biz, article_id, title, link, raw_json, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, '{}', NOW(), NOW())""",
                (pk, biz, f'article-{pk}', f'Article {pk}', f'https://mp.weixin.qq.com/s/article-{pk}'),
            )
            await self.conn.execute(
                """INSERT INTO article_content (article_pk, content_json, content_markdown, created_at, updated_at)
                   VALUES (%s, '[]'::jsonb, '', NOW(), NOW())""",
                (pk,),
            )
        # A direct reference grants article 2 to user 7 without a subscription to
        # MzForeign, exactly like the reference path writes it.
        await self.conn.execute("""
            INSERT INTO article_reference (source_article_pk, source_updated_at, url, identity, target_article_pk)
            SELECT 1, c.updated_at, 'https://mp.weixin.qq.com/s/article-2', 'article-2', 2
              FROM article_content c WHERE c.article_pk = 1
        """)
        await self.conn.execute("SELECT setval(pg_get_serial_sequence('articles', 'id'), 3)")
        await self.conn.set_autocommit(False)
        self.storage = self._storage(self.conn)

    def _storage(self, conn: psycopg.AsyncConnection) -> PostgresStorage:
        storage = PostgresStorage(DSN)
        storage.conn = conn
        storage._bind_repositories()
        # Topics are enrichment no case here asserts on; leaving the repository
        # out keeps the fixture to the tables under test (``_attach_topics``
        # guards with getattr).
        storage.topics = None
        return storage

    async def second_reader_storage(self) -> PostgresStorage:
        """A second connection to the same schema, owning its own transactions.

        ``connect`` defaults to ``autocommit=False``, so ``SET search_path`` would
        already open a transaction and ``set_autocommit`` then refuses to change
        it; the connection is opened in autocommit mode for the SET and only then
        put into the manual-transaction mode the storage handle expects.
        """
        conn = await psycopg.AsyncConnection.connect(DSN, autocommit=True)
        self.addAsyncCleanup(conn.close)
        await conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        await conn.set_autocommit(False)
        return self._storage(conn)

    async def cleanup(self):
        await self.conn.rollback()
        await self.conn.set_autocommit(True)
        await self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        await self.conn.close()

    async def count(self, query: str, params: tuple[object, ...] = ()) -> int:
        row = await (await self.conn.execute(query, params)).fetchone()
        return int(row[0])

    async def wait_for_a_blocked_backend(self, timeout: float = 5.0) -> bool:
        """True once another connection of this test is waiting on a lock."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            waiting = await self.count(
                'SELECT count(*) FROM pg_stat_activity'
                ' WHERE datname = current_database() AND pid <> pg_backend_pid()'
                " AND wait_event_type = 'Lock'"
            )
            if waiting:
                return True
            await asyncio.sleep(0.05)
        return False

    async def append(self, storage: PostgresStorage, user_id: int, article_pk: int) -> str:
        async with storage.transaction():
            return await storage.watch_later.add(user_id=user_id, article_pk=article_pk)

    async def test_appending_twice_keeps_the_first_time_and_position(self):
        first = await self.append(self.storage, 7, 1)
        second = await self.append(self.storage, 7, 1)
        await self.append(self.storage, 7, 2)

        self.assertEqual(first, second)
        entries = await self.storage.watch_later.entries(user_id=7)
        self.assertEqual([1, 2], [entry['article_pk'] for entry in entries])
        self.assertEqual([1, 2], [entry['position'] for entry in entries])
        self.assertEqual(
            {1: entries[0]['added_at'].isoformat(), 2: entries[1]['added_at'].isoformat()},
            await self.storage.watch_later.added_at_for(user_id=7, article_pks=[1, 2, 3]),
        )
        # Every reader has their own list; the same article can sit in both.
        self.assertEqual({}, await self.storage.watch_later.added_at_for(user_id=8, article_pks=[1, 2]))

    async def test_removing_twice_is_harmless(self):
        await self.append(self.storage, 7, 1)
        for _ in range(2):
            async with self.storage.transaction():
                await self.storage.watch_later.remove(user_id=7, article_pk=1)

        self.assertEqual(0, await self.count('SELECT count(*) FROM article_watch_later WHERE user_id = 7'))

    async def test_moving_rewrites_only_the_given_rows(self):
        await self.append(self.storage, 7, 1)
        await self.append(self.storage, 7, 2)
        async with self.storage.transaction():
            await self.storage.watch_later.set_positions(user_id=7, assignments=[(2, 1), (1, 2)])

        entries = await self.storage.watch_later.entries(user_id=7)
        self.assertEqual([2, 1], [entry['article_pk'] for entry in entries])

    async def test_concurrent_appends_from_one_reader_take_distinct_positions(self):
        other = await self.second_reader_storage()

        # Both appends read MAX(position); the users row lock is what makes them
        # take turns instead of picking the same position.
        results = await asyncio.wait_for(
            asyncio.gather(
                self.append(self.storage, 7, 1),
                self.append(other, 7, 2),
            ),
            timeout=10,
        )

        self.assertEqual(2, len(results))
        entries = await self.storage.watch_later.entries(user_id=7)
        self.assertEqual([1, 2], sorted(entry['article_pk'] for entry in entries))
        self.assertEqual([1, 2], sorted(entry['position'] for entry in entries))

    async def test_a_concurrent_move_waits_for_the_lock_before_reading_the_list(self):
        for article_pk in (1, 2):
            await self.append(self.storage, 7, article_pk)

        other = await self.second_reader_storage()

        async def move(storage: PostgresStorage, article_pk: int, direction: str) -> None:
            async with storage.transaction():
                await _move_watch_later(storage, 7, article_pk, direction)

        pending: asyncio.Task | None = None
        try:
            async with self.storage.transaction():
                # The first transaction reads the list under the lock and then
                # publishes its own order; the second one must not have taken its
                # snapshot yet, or it would write back the order it saw before.
                await self.storage.watch_later.lock(user_id=7)
                entries = await self.storage.watch_later.entries(user_id=7)
                self.assertEqual([1, 2], [entry['article_pk'] for entry in entries])

                pending = asyncio.create_task(move(other, 2, 'down'))
                self.assertTrue(
                    await self.wait_for_a_blocked_backend(),
                    'the concurrent move never waited for the reader lock',
                )
                self.assertFalse(pending.done())
                await self.storage.watch_later.set_positions(user_id=7, assignments=[(2, 1), (1, 2)])
            await asyncio.wait_for(pending, timeout=10)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending

        # Read after the commit: [2, 1]. Moving 2 down therefore lands on [1, 2].
        # A snapshot taken before the lock would have seen [1, 2], where moving 2
        # down is a no-op, and would have left [2, 1] behind.
        entries = await self.storage.watch_later.entries(user_id=7)
        self.assertEqual([1, 2], [entry['article_pk'] for entry in entries])

    async def test_the_list_keeps_subscribed_and_directly_referenced_articles_only(self):
        for article_pk in (1, 2, 3):
            await self.append(self.storage, 7, article_pk)

        # Article 1 comes from a subscription, article 2 from a direct reference,
        # article 3 from neither: the same gate the article route applies.
        await _assert_article_visible(self.storage, 7, 1)
        await _assert_article_visible(self.storage, 7, 2)
        with self.assertRaises(ApiError):
            await _assert_article_visible(self.storage, 7, 3)

        listed = await _list_watch_later(self.storage, 7)
        self.assertEqual(2, listed['total'])
        self.assertEqual([1, 2], [row['id'] for row in listed['articles']])
        self.assertIsInstance(listed['articles'][0]['watch_later_at'], str)
        # Another reader sees none of it.
        self.assertEqual({'articles': [], 'total': 0}, await _list_watch_later(self.storage, 8))

    async def test_a_move_steps_over_an_entry_the_caller_cannot_see(self):
        for article_pk in (1, 2, 3):
            await self.append(self.storage, 7, article_pk)

        async with self.storage.transaction():
            await _move_watch_later(self.storage, 7, 2, 'up')
        listed = await _list_watch_later(self.storage, 7)
        self.assertEqual([2, 1], [row['id'] for row in listed['articles']])
        # The invisible entry keeps its position, so the move did not touch it and
        # moving it is a 404 rather than a silent reshuffle.
        with self.assertRaises(ApiError):
            async with self.storage.transaction():
                await _move_watch_later(self.storage, 7, 3, 'top')
        self.assertEqual(1, await self.count('SELECT count(*) FROM article_watch_later WHERE article_pk = 3'))

    async def test_deleting_an_article_removes_its_watch_later_entries(self):
        await self.append(self.storage, 7, 1)
        await self.append(self.storage, 8, 1)
        await self.append(self.storage, 7, 2)

        # The user asked for the article foreign key to cascade, so deleting an
        # article must not leave a row pointing at nothing.
        await self.conn.execute('DELETE FROM articles WHERE id = 1')
        await self.conn.commit()

        self.assertEqual(0, await self.count('SELECT count(*) FROM article_watch_later WHERE article_pk = 1'))
        self.assertEqual(1, await self.count('SELECT count(*) FROM article_watch_later'))

    async def test_deleting_a_reader_removes_their_list_only(self):
        await self.append(self.storage, 7, 1)
        await self.append(self.storage, 8, 1)

        await self.conn.execute('DELETE FROM users WHERE id = 7')
        await self.conn.commit()

        self.assertEqual(0, await self.count('SELECT count(*) FROM article_watch_later WHERE user_id = 7'))
        self.assertEqual(1, await self.count('SELECT count(*) FROM article_watch_later WHERE user_id = 8'))
