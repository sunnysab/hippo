"""Execute queue transitions against SQLite; PostgreSQL row locks are not simulated.

Only PostgreSQL casts, placeholder syntax, interval arithmetic, RETURNING aliases,
function defaults and the row-lock clause are translated. This checks CAS/state behavior, not concurrent SKIP LOCKED
visibility, which still needs validation on an isolated PostgreSQL instance.
"""

import json
import sqlite3
import time
import unittest
from pathlib import Path
from uuid import UUID

from psycopg.types.json import Jsonb

from hippo.repositories.outline import OutlineRepository


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
        sql = sql.replace(' FOR UPDATE SKIP LOCKED', '').replace('RETURNING o.*', 'RETURNING *')
        values = tuple(
            json.dumps(v.obj) if isinstance(v, Jsonb) else str(v) if isinstance(v, UUID) else v for v in values
        )
        self.cursor.execute(sql, values)

    async def fetchone(self):
        row = self.cursor.fetchone()
        if row is None:
            return None
        result = dict(row)
        if 'sections' in result:
            result['sections'] = json.loads(result['sections'])
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
        table = schema.split('CREATE TABLE IF NOT EXISTS article_outline (', 1)[1].split('\n);', 1)[0]
        self.db.executescript(
            """
            CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT);
            CREATE TABLE article_content (article_pk INTEGER PRIMARY KEY, content_json TEXT, updated_at TEXT);
            INSERT INTO articles VALUES (1, 'Title');
        """
            + 'CREATE TABLE article_outline ('
            + table.replace('::jsonb', '').replace('DEFAULT clock_timestamp()', 'DEFAULT (clock_timestamp())')
            + '\n);'
        )

    def cursor(self, **kwargs):
        return Cursor(self)


class OutlineRepositoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = Connection()
        self.repo = OutlineRepository(self.connection)
        self.addCleanup(self.connection.db.close)

    async def test_same_snapshot_deduplicates_queued_and_running_requests(self):
        await self.repo.enqueue(1, 'hash')
        claim = await self.repo.claim()
        for force in (False, True):
            row = await self.repo.enqueue(1, 'hash', force=force)
            self.assertEqual('processing', row['state'])
            self.assertEqual(claim['lease_token'], row['lease_token'])
        self.assertIsNone(await self.repo.claim())

    async def test_ready_cache_is_reused_until_force(self):
        await self.repo.enqueue(1, 'hash')
        claim = await self.repo.claim()
        sections = [{'title': 'Topic', 'start_block': 0, 'children': []}]
        self.assertTrue(await self.repo.finish(claim, sections=sections, model='test'))
        row = await self.repo.enqueue(1, 'hash')
        self.assertEqual('ready', row['state'])
        self.assertEqual(sections, row['sections'])
        self.assertIsNone(row['lease_token'])
        row = await self.repo.enqueue(1, 'hash', force=True)
        self.assertEqual('queued', row['state'])
        self.assertEqual([], row['sections'])
        self.assertIsNone(row['model'])

    async def test_a_replaced_snapshot_rejects_the_previous_worker_result(self):
        await self.repo.enqueue(1, 'old')
        old = await self.repo.claim()
        await self.repo.enqueue(1, 'new')
        self.assertFalse(await self.repo.finish(old, sections=[{'title': 'Old'}]))
        self.assertEqual('new', (await self.repo.get(1))['fingerprint'])
        self.assertEqual('queued', (await self.repo.get(1))['state'])

    async def test_expired_leases_are_reclaimed_with_a_new_token(self):
        await self.repo.enqueue(1, 'hash')
        old = await self.repo.claim()
        self.connection.db.execute('UPDATE article_outline SET updated_at = ?', (time.time() - 901,))
        new = await self.repo.claim()
        self.assertNotEqual(old['lease_token'], new['lease_token'])
        self.assertFalse(await self.repo.finish(old, error='outline.generation_failed'))
        self.assertTrue(await self.repo.finish(new, sections=[]))
        self.assertTrue(any('FOR UPDATE SKIP LOCKED' in sql for sql in self.connection.queries))

    async def test_failed_jobs_can_be_retried_without_force(self):
        await self.repo.enqueue(1, 'hash')
        claim = await self.repo.claim()
        await self.repo.finish(claim, error='outline.invalid_result')
        self.assertEqual('failed', (await self.repo.get(1))['state'])
        row = await self.repo.enqueue(1, 'hash')
        self.assertEqual('queued', row['state'])
        self.assertIsNone(row['error_code'])

    async def test_only_the_current_lease_can_be_released(self):
        await self.repo.enqueue(1, 'hash')
        claim = await self.repo.claim()
        await self.repo.release(dict(claim, lease_token='not-owned'))
        self.assertEqual('processing', (await self.repo.get(1))['state'])
        await self.repo.release(claim)
        self.assertEqual('queued', (await self.repo.get(1))['state'])
        self.assertIsNone((await self.repo.get(1))['lease_token'])

    async def test_sources_may_be_missing_content_but_not_invented_articles(self):
        self.assertIsNone(await self.repo.source(99))
        self.assertIsNone((await self.repo.source(1))['content_json'])
