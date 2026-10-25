"""Queue transitions against SQLite; PostgreSQL row locks are not simulated.

Only the PostgreSQL-only spellings are translated, exactly like the outline
counterpart. This checks the state machine and the CAS behavior, not concurrent
SKIP LOCKED visibility, which still needs an isolated PostgreSQL instance.
"""

import json
import sqlite3
import time
import unittest
from pathlib import Path
from uuid import UUID

from psycopg.types.json import Jsonb

from hippo.repositories.topic import TopicRepository


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.cursor = connection.db.cursor()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.cursor.close()

    async def execute(self, sql, values=()):
        self.connection.queries.append(sql)
        sql = sql.replace('%s', '?').replace('::jsonb', '')
        sql = sql.replace("clock_timestamp() - INTERVAL '15 minutes'", '(clock_timestamp() - 900)')
        sql = sql.replace(' FOR UPDATE SKIP LOCKED', '').replace('RETURNING t.*', 'RETURNING *')
        # The PostgreSQL empty-array literal has no SQLite equivalent.
        sql = sql.replace("slugs = '{}'", "slugs = '[]'")
        values = tuple(
            json.dumps(v.obj) if isinstance(v, Jsonb) else str(v) if isinstance(v, UUID) else v for v in values
        )
        # TEXT[] has no SQLite equivalent; store it as JSON so the round trip is checkable.
        if isinstance(values, tuple):
            values = tuple(json.dumps(v) if isinstance(v, list) else v for v in values)
        self.cursor.execute(sql, values)

    async def fetchone(self):
        row = self.cursor.fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ('topics', 'slugs'):
            if key in result and isinstance(result[key], str):
                result[key] = json.loads(result[key])
        return result

    @property
    def rowcount(self):
        return self.cursor.rowcount


class Connection:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.create_function('clock_timestamp', 0, time.time)
        self.queries = []
        schema = (Path(__file__).resolve().parents[1] / 'schema/postgres.sql').read_text()
        table = schema.split('CREATE TABLE IF NOT EXISTS article_topic (', 1)[1].split('\n);', 1)[0]
        self.db.executescript(
            """
            CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT, digest TEXT, publish_at INTEGER);
            INSERT INTO articles VALUES (1, 'Title', 'Digest', 2000);
        """
            + 'CREATE TABLE article_topic ('
            + table.replace('::jsonb', '').replace('DEFAULT clock_timestamp()', 'DEFAULT (clock_timestamp())')
            .replace("slugs TEXT[] NOT NULL DEFAULT '{}'", "slugs TEXT NOT NULL DEFAULT '[]'")
            + '\n);'
        )

    def cursor(self, **kwargs):
        return Cursor(self)


class TopicRepositoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = Connection()
        self.repo = TopicRepository(self.connection)
        self.addCleanup(self.connection.db.close)

    async def test_backfill_queues_each_article_once_and_keeps_moving(self):
        for article_pk in (2, 3, 4):
            self.connection.db.execute(
                'INSERT INTO articles VALUES (?, ?, ?, ?)', (article_pk, f'T{article_pk}', 'D', 2000)
            )

        # Four articles, two per round: the second call must pick up the rest
        # rather than re-selecting the rows it just inserted.
        self.assertEqual(2, await self.repo.backfill(since_ts=1000, vocabulary_version=1, limit=2))
        self.assertEqual(2, await self.repo.backfill(since_ts=1000, vocabulary_version=1, limit=2))
        # Nothing left: the same call must terminate rather than re-select the same rows.
        self.assertEqual(0, await self.repo.backfill(since_ts=1000, vocabulary_version=1, limit=2))
        self.assertEqual(4, self.connection.db.execute('SELECT COUNT(*) FROM article_topic').fetchone()[0])

    async def test_backfill_ignores_articles_older_than_the_window(self):
        self.connection.db.execute('INSERT INTO articles VALUES (?, ?, ?, ?)', (2, 'T', 'D', 10))
        self.assertEqual(1, await self.repo.backfill(since_ts=1000, vocabulary_version=1))
        self.assertEqual(0, await self.repo.backfill(since_ts=1000, vocabulary_version=1))
        self.assertIsNone(await self.repo.get(2))

    async def test_claim_transitions_to_processing_and_owns_a_lease(self):
        await self.repo.backfill(since_ts=1000, vocabulary_version=1)
        claim = await self.repo.claim()
        self.assertEqual('processing', claim['state'])
        self.assertTrue(claim['lease_token'])
        self.assertIsNone(await self.repo.claim())
        self.assertTrue(any('FOR UPDATE SKIP LOCKED' in sql for sql in self.connection.queries))

    async def test_finish_publishes_topics_and_the_matching_slugs(self):
        await self.repo.backfill(since_ts=1000, vocabulary_version=1)
        claim = await self.repo.claim()
        topics = [{'slug': 'ai', 'name': '人工智能'}, {'slug': 'research', 'name': '研究与论文'}]

        self.assertTrue(
            await self.repo.finish(claim, topics=topics, fingerprint='fp', vocabulary_version=1, model='m')
        )

        row = await self.repo.get(1)
        self.assertEqual('ready', row['state'])
        self.assertEqual(topics, row['topics'])
        self.assertEqual(['ai', 'research'], row['slugs'])
        self.assertEqual('fp', row['fingerprint'])
        self.assertIsNone(row['lease_token'])

    async def test_an_empty_topic_list_is_a_published_result(self):
        await self.repo.backfill(since_ts=1000, vocabulary_version=1)
        claim = await self.repo.claim()
        self.assertTrue(await self.repo.finish(claim, topics=[], fingerprint='fp', vocabulary_version=1))
        row = await self.repo.get(1)
        self.assertEqual('ready', row['state'])
        self.assertEqual([], row['topics'])
        self.assertEqual([], row['slugs'])

    async def test_a_stale_lease_can_never_publish(self):
        await self.repo.backfill(since_ts=1000, vocabulary_version=1)
        stale = await self.repo.claim()
        self.connection.db.execute('UPDATE article_topic SET updated_at = ?', (time.time() - 901,))
        current = await self.repo.claim()
        self.assertNotEqual(stale['lease_token'], current['lease_token'])

        self.assertFalse(await self.repo.finish(stale, topics=[{'slug': 'ai', 'name': 'x'}], fingerprint='old'))
        self.assertEqual('processing', (await self.repo.get(1))['state'])

        self.assertTrue(await self.repo.finish(current, topics=[], fingerprint='new'))
        self.assertEqual('ready', (await self.repo.get(1))['state'])
        self.assertEqual('new', (await self.repo.get(1))['fingerprint'])

    async def test_a_failure_is_recorded_without_publishing_partial_topics(self):
        await self.repo.backfill(since_ts=1000, vocabulary_version=1)
        claim = await self.repo.claim()
        self.assertTrue(await self.repo.finish(claim, error='topic.invalid_result'))
        row = await self.repo.get(1)
        self.assertEqual('failed', row['state'])
        self.assertEqual('topic.invalid_result', row['error_code'])
        self.assertEqual([], row['topics'])

    async def test_a_failed_row_is_not_picked_up_again_without_help(self):
        await self.repo.backfill(since_ts=1000, vocabulary_version=1)
        claim = await self.repo.claim()
        await self.repo.finish(claim, error='topic.invalid_result')
        # Backfill only adds rows that do not exist; it never resurrects a failure.
        self.assertEqual(0, await self.repo.backfill(since_ts=1000, vocabulary_version=1))
        self.assertEqual('failed', (await self.repo.get(1))['state'])

    async def test_only_the_current_lease_can_be_released(self):
        await self.repo.backfill(since_ts=1000, vocabulary_version=1)
        claim = await self.repo.claim()
        await self.repo.release(dict(claim, lease_token='not-owned'))
        self.assertEqual('processing', (await self.repo.get(1))['state'])
        await self.repo.release(claim)
        self.assertEqual('queued', (await self.repo.get(1))['state'])
        self.assertIsNone((await self.repo.get(1))['lease_token'])

    async def test_requeue_stale_moves_only_the_old_vocabulary_and_is_batched(self):
        await self.repo.backfill(since_ts=1000, vocabulary_version=1)
        claim = await self.repo.claim()
        await self.repo.finish(claim, topics=[{'slug': 'ai', 'name': 'x'}], fingerprint='fp', vocabulary_version=1)

        self.assertEqual(1, await self.repo.requeue_stale(vocabulary_version=2, limit=10))
        row = await self.repo.get(1)
        self.assertEqual('queued', row['state'])
        self.assertEqual([], row['topics'])
        self.assertEqual([], row['slugs'])
        self.assertIsNone(row['model'])
        self.assertEqual(2, row['vocabulary_version'])
        # Already current: a second pass has nothing to do.
        self.assertEqual(0, await self.repo.requeue_stale(vocabulary_version=2, limit=10))

    async def test_requeue_stale_respects_its_batch_limit(self):
        for article_pk in (2, 3, 4):
            self.connection.db.execute('INSERT INTO articles VALUES (?, ?, ?, ?)', (article_pk, f'T{article_pk}', 'D', 2000))
        await self.repo.backfill(since_ts=1000, vocabulary_version=1)
        self.assertEqual(2, await self.repo.requeue_stale(vocabulary_version=2, limit=2))
        self.assertEqual(2, await self.repo.requeue_stale(vocabulary_version=2, limit=2))

    async def test_the_source_row_is_the_only_thing_the_classifier_sees(self):
        self.assertEqual({'title': 'Title', 'digest': 'Digest'}, await self.repo.source(1))
        self.assertIsNone(await self.repo.source(99))


if __name__ == '__main__':
    unittest.main()
