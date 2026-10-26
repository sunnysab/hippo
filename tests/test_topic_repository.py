"""Queue transitions against SQLite; PostgreSQL row locks are not simulated.

Only the PostgreSQL-only spellings are translated, exactly like the outline
counterpart. This checks the state machine and the CAS behavior, not concurrent
SKIP LOCKED visibility, which still needs an isolated PostgreSQL instance.
"""

import json
import re
import sqlite3
import time
import unittest
from pathlib import Path
from typing import ClassVar
from uuid import UUID

from psycopg.types.json import Jsonb

from hippo.repositories.topic import TopicRepository


def _trigrams(text: str) -> set[str]:
    padded = f'  {text} '
    return {padded[index : index + 3] for index in range(len(padded) - 2)}


def _trigram_similarity(left: str, right: str) -> float:
    """Dice coefficient over trigrams, matching pg_trgm's ``similarity``."""
    if not left or not right:
        return 0.0
    a, b = _trigrams(left), _trigrams(right)
    return 2 * len(a & b) / (len(a) + len(b))


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
        # PostgreSQL casts have no SQLite form; the value is a bound parameter.
        sql = sql.replace('CAST(? AS text) IS NULL', '? IS NULL')
        # The publish_at fallback: SQLite stores created_at the same way, so the
        # COALESCE collapses to the column itself.
        sql = sql.replace(
            'COALESCE(a.publish_at, EXTRACT(EPOCH FROM a.created_at)::bigint)',
            'COALESCE(a.publish_at, a.created_at)',
        )
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
        # pg_trgm's similarity() is the trigram Dice coefficient; SQLite has no
        # trigram extension, so the same formula is registered here. The tests
        # assert on candidate membership, never on a specific score.
        self.db.create_function('similarity', 2, _trigram_similarity)
        self.queries = []
        schema = (Path(__file__).resolve().parents[1] / 'schema/postgres.sql').read_text()
        table = schema.split('CREATE TABLE IF NOT EXISTS article_topic (', 1)[1].split('\n);', 1)[0]
        self.db.executescript(
            """
            CREATE TABLE accounts (biz TEXT PRIMARY KEY, nickname TEXT);
            -- Every fixture article is subscribed by user 1 unless a test says otherwise.
            CREATE TABLE subscription (
                user_id INTEGER NOT NULL, biz TEXT NOT NULL, is_disabled BOOLEAN NOT NULL DEFAULT FALSE,
                PRIMARY KEY (user_id, biz));
            CREATE TABLE articles (
                id INTEGER PRIMARY KEY, title TEXT, digest TEXT, publish_at INTEGER, biz TEXT,
                created_at INTEGER
            );
            INSERT INTO articles VALUES (1, 'Title', 'Digest', 2000, 'biz_a', 2000);
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
                'INSERT INTO articles VALUES (?, ?, ?, ?, ?, ?)', (article_pk, f'T{article_pk}', 'D', 2000, 'biz_a', 2000)
            )

        # Four articles, two per round: the second call must pick up the rest
        # rather than re-selecting the rows it just inserted.
        self.assertEqual(2, await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1, limit=2))
        self.assertEqual(2, await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1, limit=2))
        # Nothing left: the same call must terminate rather than re-select the same rows.
        self.assertEqual(0, await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1, limit=2))
        self.assertEqual(4, self.connection.db.execute('SELECT COUNT(*) FROM article_topic').fetchone()[0])

    async def test_backfill_ignores_articles_older_than_the_window(self):
        self.connection.db.execute('INSERT INTO articles VALUES (?, ?, ?, ?, ?, ?)', (2, 'T', 'D', 10, 'biz_a', 10))
        self.assertEqual(1, await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1))
        self.assertEqual(0, await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1))
        self.assertIsNone(await self.repo.get(2))

    async def test_claim_transitions_to_processing_and_owns_a_lease(self):
        await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1)
        claim = await self.repo.claim()
        self.assertEqual('processing', claim['state'])
        self.assertTrue(claim['lease_token'])
        self.assertIsNone(await self.repo.claim())
        self.assertTrue(any('FOR UPDATE SKIP LOCKED' in sql for sql in self.connection.queries))

    async def test_finish_publishes_topics_and_the_matching_slugs(self):
        await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1)
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
        await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1)
        claim = await self.repo.claim()
        self.assertTrue(await self.repo.finish(claim, topics=[], fingerprint='fp', vocabulary_version=1))
        row = await self.repo.get(1)
        self.assertEqual('ready', row['state'])
        self.assertEqual([], row['topics'])
        self.assertEqual([], row['slugs'])

    async def test_a_stale_lease_can_never_publish(self):
        await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1)
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
        await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1)
        claim = await self.repo.claim()
        self.assertTrue(await self.repo.finish(claim, error='topic.invalid_result'))
        row = await self.repo.get(1)
        self.assertEqual('failed', row['state'])
        self.assertEqual('topic.invalid_result', row['error_code'])
        self.assertEqual([], row['topics'])

    async def test_a_failed_row_is_not_picked_up_again_without_help(self):
        await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1)
        claim = await self.repo.claim()
        await self.repo.finish(claim, error='topic.invalid_result')
        # Backfill only adds rows that do not exist; it never resurrects a failure.
        self.assertEqual(0, await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1))
        self.assertEqual('failed', (await self.repo.get(1))['state'])

    async def test_only_the_current_lease_can_be_released(self):
        await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1)
        claim = await self.repo.claim()
        await self.repo.release(dict(claim, lease_token='not-owned'))
        self.assertEqual('processing', (await self.repo.get(1))['state'])
        await self.repo.release(claim)
        self.assertEqual('queued', (await self.repo.get(1))['state'])
        self.assertIsNone((await self.repo.get(1))['lease_token'])

    async def test_requeue_stale_moves_only_the_old_vocabulary_and_is_batched(self):
        await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1)
        claim = await self.repo.claim()
        await self.repo.finish(claim, topics=[{'slug': 'ai', 'name': 'x'}], fingerprint='fp', vocabulary_version=1)

        self.assertEqual(1, await self.repo.requeue_stale(vocabulary_version=2, prompt_version=1, limit=10))
        row = await self.repo.get(1)
        self.assertEqual('queued', row['state'])
        self.assertEqual([], row['topics'])
        self.assertEqual([], row['slugs'])
        self.assertIsNone(row['model'])
        self.assertEqual(2, row['vocabulary_version'])
        # Already current: a second pass has nothing to do.
        self.assertEqual(0, await self.repo.requeue_stale(vocabulary_version=2, prompt_version=1, limit=10))

    async def test_requeue_stale_respects_its_batch_limit(self):
        for article_pk in (2, 3, 4):
            self.connection.db.execute('INSERT INTO articles VALUES (?, ?, ?, ?, ?, ?)', (article_pk, f'T{article_pk}', 'D', 2000, 'biz_a', 2000))
        await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1)
        self.assertEqual(2, await self.repo.requeue_stale(vocabulary_version=2, prompt_version=1, limit=2))
        self.assertEqual(2, await self.repo.requeue_stale(vocabulary_version=2, prompt_version=1, limit=2))

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
            'INSERT OR IGNORE INTO articles (id, title, digest, publish_at, biz, created_at)'
            ' VALUES (?, ?, ?, ?, ?, 2000)', (pk, f'T{pk}', 'D', 2000, biz)
        )
        self.connection.db.execute('INSERT OR IGNORE INTO accounts VALUES (?, ?)', (biz, biz))
        self.connection.db.execute('UPDATE articles SET biz = ? WHERE id = ?', (biz, pk))
        # Reader 1 follows every fixture publisher; scoping tests remove some.
        self.connection.db.execute(
            'INSERT OR IGNORE INTO subscription (user_id, biz) VALUES (1, ?)', (biz,)
        )

    def _link(self, pk, event_id):
        self.connection.db.execute(
            'INSERT INTO article_event_link (article_pk, event_id) VALUES (?, ?)', (pk, event_id)
        )

    async def test_a_new_event_is_offered_as_a_candidate_before_it_is_visible(self):
        """The loop that made phase two unreachable.

        A new event has one publisher, so it is not visible yet. If the candidate
        list applied the same threshold, no second article could ever join it and
        no event could ever become visible -- the feature would be dead code. This
        pins the two rules apart: visibility is a reader-facing gate, candidacy is
        how an event earns that visibility.
        """
        self._article(1, 'biz_a')
        event_id = await self.repo.create_event('某事发生', 1)

        # Not yet a story for readers...
        self.assertIsNone(await self.repo.public_event(event_id, 1))
        # ...but the model must be able to see it, or nobody can ever join.
        candidates = await self.repo.candidates()
        self.assertEqual([event_id], [item['id'] for item in candidates])
        self.assertEqual(1, candidates[0]['sources'])

        self._article(2, 'biz_b')
        await self.repo.attach_event(2, event_id)

        visible = await self.repo.public_event(event_id, 1)
        self.assertEqual(2, visible['source_count'])
        self.assertEqual(2, visible['article_count'])

    async def test_joining_a_candidate_is_what_makes_an_event_visible(self):
        """End to end over the real repository: create, offer, join, become visible."""
        self._article(1, 'biz_a')
        event_id = await self.repo.create_event('某事发生', 1)
        self.assertIsNone(await self.repo.public_event(event_id, 1))

        # A second publisher joins *through the candidate list*, the only path the
        # worker has. Nothing here calls attach_event out of band.
        offered = await self.repo.candidates()
        target = next(item['id'] for item in offered if item['id'] == event_id)
        self._article(2, 'biz_b')
        await self.repo.attach_event(2, target)

        visible = await self.repo.public_event(event_id, 1)
        self.assertIsNotNone(visible)
        self.assertEqual(2, visible['source_count'])

    async def test_two_articles_from_the_same_publisher_are_not_a_story(self):
        for pk in (1, 2, 3):
            self._article(pk, 'biz_a')
        event_id = await self.repo.create_event('刷屏', 1)
        await self.repo.attach_event(2, event_id)
        await self.repo.attach_event(3, event_id)

        self.assertIsNone(await self.repo.public_event(event_id, 1))

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

    async def _crowded_events(self) -> tuple[int, int]:
        """Far more events than the candidate limit, split into wide and fresh ones.

        Forty stories with three publishers each outnumber any count-based short
        list; ten newer single-publisher stories outnumber any recency ranking. A
        fixture where the wide stories tie cannot show which one survived, so their
        publisher counts differ.
        """
        pk = 1
        widest = 0
        # Publisher counts shrink with age's opposite: the first story is the widest
        # and the later ones narrower, so "the widest" is a single identifiable row.
        for index in range(1, 41):
            publishers = 40 - index + 3
            for offset in range(publishers):
                self._article(pk, f'wide_{index}_{offset}')
                if offset == 0:
                    event_id = await self.repo.create_event(f'大事件 {index}', pk)
                else:
                    await self.repo.attach_event(pk, event_id)
                pk += 1
            if index == 1:
                widest = event_id
        fresh = []
        for index in range(10):
            self._article(pk, f'fresh_{index}')
            fresh.append(await self.repo.create_event(f'新事 {index}', pk))
            pk += 1
        return widest, fresh[-1]

    async def test_a_fresh_event_is_offered_alongside_the_widely_covered_ones(self):
        """A story one minute old must still be offered, or it can never be joined.

        Measured on production: twelve publishers covering one earthquake produced
        twelve separate events. Every candidate slot went to stories that already
        had more publishers, and a single-publisher event sorted past position
        2,646 -- so the second article could never see the first.
        """
        _, newest = await self._crowded_events()
        offered = [item['id'] for item in await self.repo.candidates()]
        self.assertIn(newest, offered, 'a new event must be offered or nothing can join it')

    async def test_a_similar_title_reaches_an_event_neither_slice_can_see(self):
        """The production failure this exists for.

        Twelve publishers covered one earthquake within four minutes, producing
        twelve events. None was among the largest stories, and all had aged out of
        the recent slice by the time a full requeue reached them, so a list built
        from counts and recency offered nothing and the split could never close.
        Only the text identifies these as one story.
        """
        for pk in range(1, 201):
            self._article(pk, f'biz_{pk}')
        # A story with one publisher -- the shape a newly opened event has -- and
        # buried under two hundred newer, wider stories.
        buried = await self.repo.create_event('习近平对西藏那曲市巴青县7.0级地震作出重要指示', 1)
        for pk in range(2, 201):
            event_id = await self.repo.create_event(f'别的新闻 {pk}', pk)
            for offset in range(1, 4):
                extra = 1000 + pk * 3 + offset
                self._article(extra, f'wide_{extra}')
                await self.repo.attach_event(extra, event_id)

        offered = await self.repo.candidates(title='西藏那曲市巴青县7.0级地震最新消息')
        self.assertIn(buried, [item['id'] for item in offered])

    async def test_without_a_title_the_similar_slice_is_skipped(self):
        """A caller with no text gets counts and recency, not a guessed score."""
        for pk in range(1, 41):
            self._article(pk, f'biz_{pk}')
        buried = await self.repo.create_event('西藏那曲地震', 1)
        for pk in range(2, 41):
            await self.repo.create_event(f'别的新闻 {pk}', pk)

        offered = await self.repo.candidates()
        self.assertNotIn(buried, [item['id'] for item in offered])

    async def test_a_widely_covered_event_is_offered_alongside_the_fresh_ones(self):
        """Half the slots are the largest stories: recency alone drops them.

        On production, ordering only by recency put the 24-publisher story at
        position 46 and a 10-publisher one at 865.
        """
        widest, _ = await self._crowded_events()
        offered = [item['id'] for item in await self.repo.candidates()]
        self.assertIn(widest, offered)

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
        self.assertIsNone(await self.repo.public_event(first, 1))
        self.assertEqual(2, (await self.repo.public_event(second, 1))['article_count'])

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
        await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1)
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

        await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1)
        claim = await self.repo.claim()
        await self.repo.finish(claim, topics=[], fingerprint='fp', vocabulary_version=1, event=None)

        self.assertIsNone(await self.repo.event_of(1))

    async def test_keep_event_protects_the_link_when_the_model_was_not_asked(self):
        self._article(1, 'a')
        old = await self.repo.create_event('甲', 1)

        await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1)
        claim = await self.repo.claim()
        await self.repo.finish(claim, topics=[], fingerprint='fp', vocabulary_version=1, keep_event=True)

        self.assertEqual(old, (await self.repo.event_of(1))['id'])

    async def test_a_failed_classification_never_touches_the_event(self):
        self._article(1, 'a')
        old = await self.repo.create_event('甲', 1)
        await self.repo.backfill(since_ts=1000, vocabulary_version=1, prompt_version=1)
        claim = await self.repo.claim()
        await self.repo.finish(claim, error='topic.invalid_result', event={'action': 'new', 'title': '不该写'})

        self.assertEqual(old, (await self.repo.event_of(1))['id'])
        self.assertEqual(1, self.connection.db.execute('SELECT COUNT(*) FROM article_event').fetchone()[0])


class PromptVersionRequeueTest(unittest.IsolatedAsyncioTestCase):
    """Bumping PROMPT_VERSION must actually requeue, not just change the fingerprint."""

    def setUp(self):
        self.connection = Connection()
        self.repo = TopicRepository(self.connection)
        self.addCleanup(self.connection.db.close)

    async def _ready_row(self, *, vocabulary_version: int, prompt_version: int):
        await self.repo.backfill(
            since_ts=1000, vocabulary_version=vocabulary_version, prompt_version=prompt_version
        )
        claim = await self.repo.claim()
        await self.repo.finish(
            claim,
            topics=[{'slug': 'ai', 'name': '人工智能'}],
            fingerprint='fp',
            vocabulary_version=vocabulary_version,
            prompt_version=prompt_version,
        )

    async def test_a_prompt_bump_requeues_even_when_the_vocabulary_is_unchanged(self):
        await self._ready_row(vocabulary_version=1, prompt_version=1)

        # Same vocabulary, newer prompt: the only thing that changed is the prompt.
        self.assertEqual(1, await self.repo.requeue_stale(vocabulary_version=1, prompt_version=2))

        row = await self.repo.get(1)
        self.assertEqual('queued', row['state'])
        self.assertEqual(2, row['prompt_version'])

    async def test_a_prompt_bump_is_recorded_when_the_row_is_reclassified(self):
        await self._ready_row(vocabulary_version=1, prompt_version=1)
        await self.repo.requeue_stale(vocabulary_version=1, prompt_version=2)

        claim = await self.repo.claim()
        await self.repo.finish(
            claim, topics=[], fingerprint='fp2', vocabulary_version=1, prompt_version=2
        )

        self.assertEqual(2, (await self.repo.get(1))['prompt_version'])
        # Nothing is stale any more, so the next pass has no work.
        self.assertEqual(0, await self.repo.requeue_stale(vocabulary_version=1, prompt_version=2))

    async def test_a_vocabulary_bump_alone_still_requeues(self):
        await self._ready_row(vocabulary_version=1, prompt_version=1)
        self.assertEqual(1, await self.repo.requeue_stale(vocabulary_version=2, prompt_version=1))

class EventSubscriptionScopeTest(unittest.IsolatedAsyncioTestCase):
    """Counts answer "what can this reader open", and the threshold uses that number."""

    def setUp(self):
        self.connection = Connection()
        self.repo = TopicRepository(self.connection)
        self.addCleanup(self.connection.db.close)

    def _article(self, pk, biz):
        self.connection.db.execute('INSERT OR IGNORE INTO accounts VALUES (?, ?)', (biz, biz))
        self.connection.db.execute(
            'INSERT OR REPLACE INTO articles VALUES (?, ?, ?, ?, ?, ?)', (pk, f'T{pk}', 'D', 2000, biz, 2000)
        )

    def _subscribe(self, user_id, biz):
        self.connection.db.execute('INSERT INTO subscription (user_id, biz) VALUES (?, ?)', (user_id, biz))

    async def _event_with_three_publishers(self) -> int:
        for pk, biz in ((1, 'a'), (2, 'b'), (3, 'c')):
            self._article(pk, biz)
            self._subscribe(1, biz)
            self._subscribe(2, biz)
        event_id = await self.repo.create_event('三家都在报', 1)
        await self.repo.attach_event(2, event_id)
        await self.repo.attach_event(3, event_id)
        return event_id

    async def test_counts_are_scoped_to_what_the_reader_can_open(self):
        event_id = await self._event_with_three_publishers()
        # Reader 2 drops two of the three publishers.
        for biz in ('b', 'c'):
            self.connection.db.execute('DELETE FROM subscription WHERE user_id = 2 AND biz = ?', (biz,))

        self.assertEqual(3, (await self.repo.public_event(event_id, 1))['article_count'])

        # One report left, so the story is not visible to reader 2 at all: telling
        # them "3 reports" and then listing one would be the leak this prevents.
        self.assertIsNone(await self.repo.public_event(event_id, 2))

    async def test_the_threshold_counts_only_the_readers_own_publishers(self):
        event_id = await self._event_with_three_publishers()
        for biz in ('b', 'c'):
            self.connection.db.execute('DELETE FROM subscription WHERE user_id = 2 AND biz = ?', (biz,))

        self.assertEqual(3, (await self.repo.public_event(event_id, 1))['source_count'])
        self.assertIsNone(await self.repo.public_event(event_id, 2))

    async def test_two_of_the_readers_publishers_are_enough(self):
        event_id = await self._event_with_three_publishers()
        self.connection.db.execute("DELETE FROM subscription WHERE user_id = 2 AND biz = 'c'")

        visible = await self.repo.public_event(event_id, 2)
        self.assertEqual(2, visible['source_count'])
        self.assertEqual(2, visible['article_count'])

    async def test_a_disabled_subscription_is_not_counted(self):
        event_id = await self._event_with_three_publishers()
        self.connection.db.execute("UPDATE subscription SET is_disabled = TRUE WHERE user_id = 2 AND biz = 'b'")

        # Two visible publishers left (a and c): still above the threshold...
        visible = await self.repo.public_event(event_id, 2)
        self.assertEqual(2, visible['source_count'])
        # ...and the disabled one is not counted in the report total either.
        self.assertEqual(2, visible['article_count'])

    async def test_the_candidate_list_stays_global(self):
        """Clustering must see every publisher; only readers are scoped."""
        event_id = await self._event_with_three_publishers()
        for biz in ('b', 'c'):
            self.connection.db.execute('DELETE FROM subscription WHERE user_id = 2 AND biz = ?', (biz,))

        # Even though reader 2 can see only one report, the model is still offered
        # the event, because whether it is a repeat is not a question about reader 2.
        self.assertEqual([event_id], [item['id'] for item in await self.repo.candidates()])

class PublishAtFallbackTest(unittest.IsolatedAsyncioTestCase):
    """A source without a publish date must still be classified."""

    def setUp(self):
        self.connection = Connection()
        self.repo = TopicRepository(self.connection)
        self.addCleanup(self.connection.db.close)

    async def test_an_article_without_publish_at_is_queued_by_its_created_at(self):
        # Some RSS feeds carry no publication date; created_at is all there is.
        self.connection.db.execute(
            'INSERT INTO articles (id, title, digest, publish_at, biz, created_at)'
            ' VALUES (2, ?, ?, NULL, ?, ?)',
            ('无日期的文章', '摘要', 'biz_a', 1792916311),
        )

        self.assertEqual(1, await self.repo.backfill(since_ts=1792000000, vocabulary_version=1, prompt_version=1))

        row = await self.repo.get(2)
        self.assertIsNotNone(row, 'an article with no publish_at was silently dropped')

    async def test_an_old_article_without_publish_at_stays_out_of_the_window(self):
        self.connection.db.execute(
            'INSERT INTO articles (id, title, digest, publish_at, biz, created_at)'
            ' VALUES (3, ?, ?, NULL, ?, ?)',
            ('很久以前', '摘要', 'biz_a', 1000),
        )
        self.assertEqual(0, await self.repo.backfill(
            since_ts=1792000000, vocabulary_version=1, prompt_version=1
        ))


class SqlPortabilityTest(unittest.TestCase):
    """Guards the SQLite double's blind spots.

    The double translates PostgreSQL spellings rather than running them, so a
    statement can pass here and still be rejected by PostgreSQL. That is exactly
    how a reserved word in a CTE name reached production and marked 9,828 rows
    failed: SQLite accepts ``similar`` as an identifier, PostgreSQL does not.
    """

    #: PostgreSQL keywords that cannot name a CTE without quoting.
    RESERVED: ClassVar[set[str]] = {'similar', 'all', 'any', 'both', 'each', 'except', 'from', 'group',
                'having', 'intersect', 'into', 'limit', 'minus', 'order', 'select',
                'union', 'where', 'window', 'with'}

    def test_cte_names_avoid_postgresql_reserved_words(self):
        source = (Path(__file__).resolve().parents[1] / 'hippo/repositories/topic.py').read_text()
        # Both spellings a CTE can take: `WITH name AS (` and `), name AS (`.
        names = {match.lower() for match in re.findall(r'(?:WITH|,)\s+(\w+)\s+AS\s+\(', source)}
        self.assertTrue(names, 'the scan found no CTEs, so it would pass vacuously')
        offenders = names & self.RESERVED
        self.assertFalse(offenders, f'reserved CTE names: {offenders}')
