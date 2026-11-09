"""Opt-in ingestion read-model and queue concurrency checks on an isolated PostgreSQL."""

import asyncio
import os
import re
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import psycopg
from psycopg import sql

from hippo.account_sync import enrich_account_sync
from hippo.api.routers.account import resume_account_backfill
from hippo.api.routers.settings import cancel_sync_task
from hippo.exceptions import ApiError
from hippo.repositories.account import AccountRepository
from hippo.sync_jobs import SyncJobRepository

DSN = os.environ.get('HIPPO_TEST_PG_DSN')


@unittest.skipUnless(DSN, 'HIPPO_TEST_PG_DSN must point at an isolated PostgreSQL test server')
class SyncStatePostgresTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = await psycopg.AsyncConnection.connect(DSN, autocommit=True)
        self.schema = f'sync_state_{uuid.uuid4().hex}'
        await self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        await self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.cleanup)
        schema = (Path(__file__).resolve().parent.parent / 'schema/postgres.sql').read_text()
        for table in ('account_groups', 'accounts', 'users', 'subscription', 'sync_jobs', 'article_queue'):
            ddl = re.search(rf'CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);', schema, re.DOTALL)
            await self.conn.execute(ddl.group())
        await self.conn.execute("ALTER TABLE accounts ADD COLUMN source text NOT NULL DEFAULT 'weixin'")
        await self.conn.execute('ALTER TABLE sync_jobs ADD COLUMN source text, ADD COLUMN requester_user_id integer')
        await self.conn.execute("""
            INSERT INTO accounts (biz, nickname, backfill_state, backfill_cursor, created_at, updated_at)
            VALUES ('one', 'One', 'pending', 'saved-cursor', NOW(), NOW());
            INSERT INTO users (id, username, password_hash, created_at, updated_at)
            VALUES (7, 'reader', 'test-only', NOW(), NOW());
            INSERT INTO subscription (user_id, biz, created_at) VALUES (7, 'one', NOW());
        """)
        self.jobs = SyncJobRepository(self.conn)

    async def cleanup(self):
        await self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        await self.conn.close()

    async def test_history_completion_keeps_body_backlog_and_check_time_independent(self):
        async with self.conn.transaction():
            job = await self.jobs.create_job(trigger_type='backfill', biz_list=['one'])
            await self.jobs.mark_running(job.task_id, worker_id='test')
        await self.conn.execute("""
            INSERT INTO article_queue (biz, sn, long_link, payload)
            VALUES ('one', 'a', 'https://test/a', '{}'), ('one', 'b', 'https://test/b', '{}');
        """)
        rows = [{'biz': 'one'}]
        await enrich_account_sync(self.conn, rows)
        self.assertEqual('running', rows[0]['sync']['history']['status'])
        self.assertEqual(2, rows[0]['sync']['articles']['pending'])
        await AccountRepository(self.conn).mark_backfill_done('one')
        await enrich_account_sync(self.conn, rows)
        self.assertEqual('complete', rows[0]['sync']['history']['status'])
        self.assertEqual(2, rows[0]['sync']['articles']['pending'])
        self.assertIsNone(rows[0]['sync']['last_checked_at'])
        await self.conn.execute("UPDATE article_queue SET state = 'done'")
        await enrich_account_sync(self.conn, rows)
        self.assertEqual(0, rows[0]['sync']['articles']['pending'])

    async def test_source_filter_keeps_legacy_and_whole_site_jobs_but_excludes_other_sources(self):
        async with self.conn.transaction():
            legacy = await self.jobs.create_job(trigger_type='backfill', biz_list=['one'])
            site = await self.jobs.create_job(trigger_type='scheduled')
            other = await self.jobs.create_job(trigger_type='manual', source='youtube')
        ids = {job.task_id for job in await self.jobs.list_jobs(source='weixin', active=True)}
        self.assertEqual({legacy.task_id, site.task_id}, ids)
        self.assertNotIn(other.task_id, ids)
        self.assertEqual([], await self.jobs.list_jobs(source='weixin', active=False))
        group = await (
            await self.conn.execute(
                "INSERT INTO account_groups (name, created_at, updated_at) VALUES ('group', NOW(), NOW()) RETURNING id"
            )
        ).fetchone()
        await self.conn.execute("UPDATE subscription SET group_id = %s WHERE biz = 'one'", (group[0],))
        async with self.conn.transaction():
            grouped = await self.jobs.create_job(trigger_type='manual', group_id=group[0])
        ids = {job.task_id for job in await self.jobs.list_jobs(source='weixin', active=True)}
        self.assertIn(grouped.task_id, ids)
        async with self.conn.transaction():
            narrowed = await self.jobs.create_job(
                trigger_type='manual', group_id=group[0], biz_list=['youtube:UC1']
            )
        ids = {job.task_id for job in await self.jobs.list_jobs(source='weixin', active=True)}
        self.assertNotIn(narrowed.task_id, ids)

    async def test_concurrent_backfill_requests_share_one_job(self):
        async def enqueue():
            async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
                await conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
                async with conn.transaction():
                    job = await SyncJobRepository(conn).create_job(
                        trigger_type='backfill', biz_list=['one'], source='weixin'
                    )
                return job.task_id

        first, second = await asyncio.gather(enqueue(), enqueue())
        self.assertEqual(first, second)
        self.assertEqual(1, len(await self.jobs.list_jobs()))

    async def test_cancelling_queued_history_pauses_without_discarding_cursor(self):
        async with self.conn.transaction():
            job = await self.jobs.create_job(trigger_type='backfill', biz_list=['one'], source='weixin')
        storage = SimpleNamespace(
            sync_jobs=self.jobs, accounts=AccountRepository(self.conn), transaction=self.conn.transaction
        )
        await cancel_sync_task(
            task_id=job.task_id,
            request=SimpleNamespace(state=SimpleNamespace(api_key=None)),
            storage=storage,
            user=SimpleNamespace(id=7, role='admin'),
        )
        row = await (
            await self.conn.execute("SELECT backfill_state, backfill_cursor FROM accounts WHERE biz = 'one'")
        ).fetchone()
        self.assertEqual(('paused', 'saved-cursor'), row)
        self.assertEqual([], await storage.accounts.list_pending_backfill(source='weixin'))
        await storage.accounts.set_backfill_state('one', 'pending')
        pending = await storage.accounts.list_pending_backfill(source='weixin')
        self.assertEqual(['one'], [account.biz for account in pending])
        await storage.accounts.mark_backfill_done('one')
        await storage.accounts.set_backfill_state('one', 'paused')
        row = await (await self.conn.execute("SELECT backfill_state FROM accounts WHERE biz = 'one'")).fetchone()
        self.assertEqual(('done',), row)

    async def test_running_cancellation_then_resume_cannot_lose_the_resume_request(self):
        accounts = AccountRepository(self.conn)
        storage = SimpleNamespace(
            sync_jobs=self.jobs, accounts=accounts, conn=self.conn, transaction=self.conn.transaction
        )
        async with self.conn.transaction():
            original = await self.jobs.create_job(trigger_type='backfill', biz_list=['one'], source='weixin')
            await self.jobs.mark_running(original.task_id, worker_id='test')
        await cancel_sync_task(
            task_id=original.task_id,
            request=SimpleNamespace(state=SimpleNamespace(api_key=None)),
            storage=storage,
            user=SimpleNamespace(id=7, role='admin'),
        )
        rows = [{'biz': 'one'}]
        await enrich_account_sync(self.conn, rows)
        self.assertEqual('cancelling', rows[0]['sync']['history']['status'])
        with patch.object(
            accounts, 'get_account_detail', AsyncMock(return_value={'source': 'weixin', 'is_disabled': False})
        ):
            with self.assertRaises(ApiError) as error:
                await resume_account_backfill('one', storage=storage, user=SimpleNamespace(id=7))
            self.assertEqual('history.cancelling', error.exception.code)
            async with self.conn.transaction():
                await self.jobs.mark_finished(original.task_id, status='cancelled', error=None, result=None)
            resumed = await resume_account_backfill('one', storage=storage, user=SimpleNamespace(id=7))
        self.assertNotEqual(original.task_id, resumed['task_id'])
        self.assertEqual('queued', resumed['status'])
        row = await (
            await self.conn.execute("SELECT backfill_state, backfill_cursor FROM accounts WHERE biz = 'one'")
        ).fetchone()
        self.assertEqual(('pending', 'saved-cursor'), row)
