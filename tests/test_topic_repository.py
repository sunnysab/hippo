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
    def __init__(self, connection, row_factory=None):
        self.connection = connection
        self.cursor = connection.db.cursor()
        # SQLite returns Row objects already; keep the flag so a caller that asks
        # for named access is honoured the way psycopg's dict_row is.
        self.named = row_factory is not None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.cursor.close()

    async def execute(self, sql, values=()):
        self.connection.queries.append(sql)
        sql = sql.replace('%s', '?').replace('::jsonb', '')
        sql = sql.replace("clock_timestamp() - INTERVAL '15 minutes'", '(clock_timestamp() - 900)')
        sql = sql.replace('clock_timestamp() - make_interval(hours => ?)', '(clock_timestamp() - (0 + ?) * 3600)')
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

    async def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]

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
            CREATE TABLE accounts (biz TEXT PRIMARY KEY, nickname TEXT);
            CREATE TABLE articles (
                id INTEGER PRIMARY KEY, title TEXT, digest TEXT, publish_at INTEGER, biz TEXT
            );
            INSERT INTO articles VALUES (1, 'Title', 'Digest', 2000, 'biz_a');
        """
            + 'CREATE TABLE article_topic ('
            + table.replace('::jsonb', '').replace('DEFAULT clock_timestamp()', 'DEFAULT (clock_timestamp())')
            .replace("slugs TEXT[] NOT NULL DEFAULT '{}'", "slugs TEXT NOT NULL DEFAULT '[]'")
            + '\n);'
        )
        event_table = schema.split('CREATE TABLE IF NOT EXISTS article_event (', 1)[1].split('\n);', 1)[0]
        link_table = schema.split('CREATE TABLE IF NOT EXISTS article_event_link (', 1)[1].split('\n);', 1)[0]
        self.db.executescript(
            'CREATE TABLE article_event ('
            + event_table.replace('DEFAULT clock_timestamp()', 'DEFAULT (clock_timestamp())')
            # SERIAL is a PostgreSQL sequence type; SQLite needs the autoincrement form.
            .replace('id SERIAL PRIMARY KEY', 'id INTEGER PRIMARY KEY AUTOINCREMENT')
            + '\n);'
            'CREATE TABLE article_event_link ('
            + link_table
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
                'INSERT INTO articles VALUES (?, ?, ?, ?, ?)', (article_pk, f'T{article_pk}', 'D', 2000, 'biz_a')
            )

        # Four articles, two per round: the second call must pick up the rest
        # rather than re-selecting the rows it just inserted.
        self.assertEqual(2, await self.repo.backfill(since_ts=1000, vocabulary_version=1, limit=2))
        self.assertEqual(2, await self.repo.backfill(since_ts=1000, vocabulary_version=1, limit=2))
        # Nothing left: the same call must terminate rather than re-select the same rows.
        self.assertEqual(0, await self.repo.backfill(since_ts=1000, vocabulary_version=1, limit=2))
        self.assertEqual(4, self.connection.db.execute('SELECT COUNT(*) FROM article_topic').fetchone()[0])

    async def test_backfill_ignores_articles_older_than_the_window(self):
        self.connection.db.execute('INSERT INTO articles VALUES (?, ?, ?, ?, ?)', (2, 'T', 'D', 10, 'biz_a'))
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
            self.connection.db.execute('INSERT INTO articles VALUES (?, ?, ?, ?, ?)', (article_pk, f'T{article_pk}', 'D', 2000, 'biz_a'))
        await self.repo.backfill(since_ts=1000, vocabulary_version=1)
        self.assertEqual(2, await self.repo.requeue_stale(vocabulary_version=2, limit=2))
        self.assertEqual(2, await self.repo.requeue_stale(vocabulary_version=2, limit=2))

    async def test_the_source_row_is_the_only_thing_the_classifier_sees(self):
        self.assertEqual({'title': 'Title', 'digest': 'Digest'}, await self.repo.source(1))
        self.assertIsNone(await self.repo.source(99))


if __name__ == '__main__':
    unittest.main()


class TopicEventTest(unittest.IsolatedAsyncioTestCase):
    """Events: one story, several publishers, and a threshold that hides the rest."""

    def setUp(self):
        self.connection = Connection()
        self.repo = TopicRepository(self.connection)
        self.addCleanup(self.connection.db.close)

    def _article(self, pk, biz):
        self.connection.db.execute(
            'INSERT OR IGNORE INTO articles VALUES (?, ?, ?, ?, ?)', (pk, f'T{pk}', 'D', 2000, biz)
        )
        self.connection.db.execute('INSERT OR IGNORE INTO accounts VALUES (?, ?)', (biz, biz))
        self.connection.db.execute('UPDATE articles SET biz = ? WHERE id = ?', (biz, pk))

    def _link(self, pk, event_id):
        self.connection.db.execute(
            'INSERT INTO article_event_link (article_pk, event_id) VALUES (?, ?)', (pk, event_id)
        )

    async def test_an_event_is_hidden_until_a_second_publisher_covers_it(self):
        self._article(1, 'biz_a')
        event_id = await self.repo.create_event('某事发生', 1)

        self.assertIsNone(await self.repo.public_event(event_id))
        self.assertEqual([], await self.repo.candidates())

        self._article(2, 'biz_b')
        await self.repo.attach_event(2, event_id)

        visible = await self.repo.public_event(event_id)
        self.assertEqual(2, visible['source_count'])
        self.assertEqual(2, visible['article_count'])
        self.assertEqual(1, len(await self.repo.candidates()))

    async def test_two_articles_from_the_same_publisher_are_not_a_story(self):
        for pk in (1, 2, 3):
            self._article(pk, 'biz_a')
        event_id = await self.repo.create_event('刷屏', 1)
        await self.repo.attach_event(2, event_id)
        await self.repo.attach_event(3, event_id)

        self.assertIsNone(await self.repo.public_event(event_id))

    async def test_candidates_rank_by_distinct_publishers_then_recency(self):
        for pk, biz in ((1, 'a'), (2, 'b'), (3, 'c'), (4, 'd')):
            self._article(pk, biz)
        wide = await self.repo.create_event('很多人报', 1)
        await self.repo.attach_event(2, wide)
        await self.repo.attach_event(3, wide)
        narrow = await self.repo.create_event('两个号', 4)
        self._article(5, 'e')
        await self.repo.attach_event(5, narrow)

        ranked = await self.repo.candidates()
        self.assertEqual([wide, narrow], [item['id'] for item in ranked])
        self.assertEqual(3, ranked[0]['sources'])

    async def test_attaching_is_an_upsert_so_a_move_is_not_a_second_link(self):
        self._article(1, 'a')
        self._article(2, 'b')
        first = await self.repo.create_event('甲', 1)
        second = await self.repo.create_event('乙', 2)

        await self.repo.attach_event(1, second)

        # Moved, not duplicated: one link row, now pointing at the newer event.
        self.assertEqual(second, (await self.repo.event_of(1))['id'])
        self.assertEqual(1, self.connection.db.execute(
            'SELECT COUNT(*) FROM article_event_link WHERE article_pk = 1').fetchone()[0])
        # The event it left is empty again, so it no longer reaches the threshold.
        self.assertIsNone(await self.repo.public_event(first))
        self.assertEqual(2, (await self.repo.public_event(second))['article_count'])

    async def test_attaching_refreshes_the_recency_window(self):
        self._article(1, 'a')
        event_id = await self.repo.create_event('甲', 1)
        self.connection.db.execute('UPDATE article_event SET last_seen_at = ?', (1.0,))
        self._article(2, 'b')
        await self.repo.attach_event(2, event_id)

        self.assertGreater(
            self.connection.db.execute('SELECT last_seen_at FROM article_event').fetchone()[0], 1.0
        )

    async def test_a_reclassification_can_move_or_drop_the_event(self):
        self._article(1, 'a')
        keep = await self.repo.create_event('甲', 1)
        self._article(2, 'b')
        await self.repo.attach_event(2, keep)
        self.assertIsNotNone(await self.repo.event_of(2))

        await self.repo.detach_event(2)
        self.assertIsNone(await self.repo.event_of(2))

    async def test_finish_applies_the_event_in_the_same_call(self):
        await self.repo.backfill(since_ts=1000, vocabulary_version=1)
        claim = await self.repo.claim()
        self.assertTrue(
            await self.repo.finish(
                claim,
                topics=[{'slug': 'ai', 'name': '人工智能'}],
                fingerprint='fp',
                vocabulary_version=1,
                event={'action': 'new', 'title': '某事发生'},
            )
        )
        self.assertEqual('某事发生', (await self.repo.event_of(1))['title'])

    async def test_a_published_topic_without_an_event_drops_the_old_link(self):
        self._article(1, 'a')
        old = await self.repo.create_event('甲', 1)
        self.assertEqual(old, (await self.repo.event_of(1))['id'])

        await self.repo.backfill(since_ts=1000, vocabulary_version=1)
        claim = await self.repo.claim()
        await self.repo.finish(claim, topics=[], fingerprint='fp', vocabulary_version=1, event=None)

        self.assertIsNone(await self.repo.event_of(1))

    async def test_keep_event_protects_the_link_when_the_model_was_not_asked(self):
        self._article(1, 'a')
        old = await self.repo.create_event('甲', 1)

        await self.repo.backfill(since_ts=1000, vocabulary_version=1)
        claim = await self.repo.claim()
        await self.repo.finish(claim, topics=[], fingerprint='fp', vocabulary_version=1, keep_event=True)

        self.assertEqual(old, (await self.repo.event_of(1))['id'])

    async def test_a_failed_classification_never_touches_the_event(self):
        self._article(1, 'a')
        old = await self.repo.create_event('甲', 1)
        await self.repo.backfill(since_ts=1000, vocabulary_version=1)
        claim = await self.repo.claim()
        await self.repo.finish(claim, error='topic.invalid_result', event={'action': 'new', 'title': '不该写'})

        self.assertEqual(old, (await self.repo.event_of(1))['id'])
        self.assertEqual(1, self.connection.db.execute('SELECT COUNT(*) FROM article_event').fetchone()[0])
