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
from hippo.storage import StorageTransaction
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
        for table in (
            'account_groups',
            'accounts',
            'users',
            'subscription',
            'sync_jobs',
            'article_queue',
            'articles',
            'article_images',
        ):
            ddl = re.search(rf'CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);', schema, re.DOTALL)
            await self.conn.execute(ddl.group())
        await self.conn.execute("ALTER TABLE accounts ADD COLUMN source text NOT NULL DEFAULT 'weixin'")
        await self.conn.execute('ALTER TABLE sync_jobs ADD COLUMN source text, ADD COLUMN requester_user_id integer')
        scope_ddl = re.search(r'ALTER TABLE sync_jobs ADD COLUMN IF NOT EXISTS scope_sources TEXT\[\];', schema).group()
        await self.conn.execute(scope_ddl)
        await self.conn.execute(scope_ddl)
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
            narrowed = await self.jobs.create_job(trigger_type='manual', group_id=group[0], biz_list=['youtube:UC1'])
        ids = {job.task_id for job in await self.jobs.list_jobs(source='weixin', active=True)}
        self.assertNotIn(narrowed.task_id, ids)

    async def test_source_history_merges_explicit_and_legacy_jobs_before_limit(self):
        await self.conn.execute("""
            INSERT INTO sync_jobs (id, status, trigger_type, source, requester_user_id, biz_list, accounts, created_at)
            VALUES
              ('explicit', 'success', 'scheduled', 'weixin', 7, NULL, '[]', '2026-01-03'),
              ('legacy', 'success', 'manual', NULL, 7, '["one"]', '[]', '2026-01-02'),
              ('snapshot', 'success', 'manual', NULL, 7, '["deleted"]',
               '[{"biz":"deleted","source":"weixin"}]', '2026-01-01'),
              ('foreign', 'success', 'manual', 'weixin', NULL, NULL, '[]', '2026-01-04');
        """)
        jobs = await self.jobs.list_jobs(source='weixin', active=False, requester_user_id=7, limit=2)
        self.assertEqual(['explicit', 'legacy'], [job.task_id for job in jobs])
        jobs = await self.jobs.list_jobs(source='weixin', active=False, requester_user_id=7, limit=3)
        self.assertEqual(['explicit', 'legacy', 'snapshot'], [job.task_id for job in jobs])

    async def test_summary_avoids_full_payload_and_preserves_enqueued_count(self):
        await self.conn.execute("""
            INSERT INTO sync_jobs (id, status, trigger_type, source, report, accounts, created_at)
            VALUES ('reported', 'success', 'scheduled', 'weixin', '{"total_saved":12,"details":[1,2]}',
                    '[{"biz":"one","saved":3}]', '2026-01-02'),
                   ('legacy-count', 'success', 'manual', 'weixin', NULL,
                    '[{"biz":"one","saved":3},{"biz":"two","saved":4}]', '2026-01-01');
        """)
        jobs = await self.jobs.list_jobs(source='weixin', detail=False)
        self.assertEqual([12, 7], [job.to_summary_dict()['total_saved'] for job in jobs])
        self.assertTrue(all(not job.accounts for job in jobs))
        self.assertTrue(all(set(job.report) == {'total_saved'} for job in jobs))
        detail = await self.jobs.get_job('reported')
        self.assertEqual(3, detail.accounts['one'].saved)
        self.assertEqual([1, 2], detail.report['details'])

    async def test_source_history_handles_large_legacy_backlog(self):
        # Most production history predates source tracking and references accounts
        # no longer present. A per-job scan of every source account is quadratic.
        await self.conn.execute("""
            INSERT INTO accounts (biz, nickname, source, created_at, updated_at)
            SELECT 'account-' || n, 'Account', 'weixin', NOW(), NOW() FROM generate_series(1, 300) n;
            INSERT INTO sync_jobs (id, status, trigger_type, biz_list, accounts, created_at)
            SELECT 'old-' || n, 'success', 'backfill', '["deleted"]', '[]', NOW()
            FROM generate_series(1, 10000) n;
        """)
        async with self.conn.transaction():
            await self.conn.execute("SET LOCAL statement_timeout = '2s'")
            self.assertEqual([], await self.jobs.list_jobs(source='weixin', active=False))

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

    async def test_image_backlog_is_separate_and_excludes_stored_images(self):
        row = await (
            await self.conn.execute("""
            INSERT INTO articles (biz, article_id, title, link, raw_json, created_at, updated_at)
            VALUES ('one', 'a', 'A', 'https://test/a', '{}', NOW(), NOW()) RETURNING id
        """)
        ).fetchone()
        for url, key, failed in [('pending', None, None), ('failed', None, 'bad'), ('stored', 'key', None)]:
            await self.conn.execute(
                """
                INSERT INTO article_images (article_pk, position, kind, orig_url, s3_key, failed_at, updated_at)
                VALUES (%s, 0, 'image', %s, %s, CASE WHEN %s::text IS NULL THEN NULL ELSE NOW() END, NOW())
            """,
                (row[0], url, key, failed),
            )
        rows = [{'biz': 'one'}]
        await enrich_account_sync(self.conn, rows)
        self.assertEqual({'pending': 1, 'failed': 1}, rows[0]['sync']['images'])

    async def test_shutdown_releases_only_owned_job_and_preserves_history_cursor(self):
        async with self.conn.transaction():
            job = await self.jobs.create_job(trigger_type='backfill', biz_list=['one'], source='weixin')
            await self.jobs.mark_running(job.task_id, worker_id='owner')
            await self.jobs.release_interrupted_job(job.task_id, worker_id='other')
        self.assertEqual('running', (await self.jobs.get_job(job.task_id)).status)
        async with self.conn.transaction():
            await self.jobs.release_interrupted_job(job.task_id, worker_id='owner')
        self.assertEqual('queued', (await self.jobs.get_job(job.task_id)).status)
        row = await (await self.conn.execute("SELECT backfill_cursor FROM accounts WHERE biz = 'one'")).fetchone()
        self.assertEqual('saved-cursor', row[0])
        async with self.conn.transaction():
            await self.jobs.mark_running(job.task_id, worker_id='owner')
            await self.jobs.cancel_job(job.task_id)
            await self.jobs.release_interrupted_job(job.task_id, worker_id='owner')
        self.assertEqual('cancelled', (await self.jobs.get_job(job.task_id)).status)

    async def test_different_sources_can_run_while_same_source_waits(self):
        async with self.conn.transaction():
            first = await self.jobs.create_job(trigger_type='scheduled', source='weixin')
            waiting = await self.jobs.create_job(trigger_type='scheduled', source='weixin')
            parallel = await self.jobs.create_job(trigger_type='scheduled', source='youtube')
            claimed = await self.jobs.claim_next_job(worker_id='one')
            await self.jobs.mark_running(claimed.task_id, worker_id='one')
        self.assertEqual(first.task_id, claimed.task_id)
        async with self.conn.transaction():
            claimed = await self.jobs.claim_next_job(worker_id='two')
            await self.jobs.mark_running(claimed.task_id, worker_id='two')
        self.assertEqual(parallel.task_id, claimed.task_id)
        async with self.conn.transaction():
            self.assertIsNone(await self.jobs.claim_next_job(worker_id='three'))
        self.assertEqual('queued', (await self.jobs.get_job(waiting.task_id)).status)

    async def test_worker_interruption_requeues_and_next_run_reads_saved_cursor(self):
        from hippo.sync_service import SyncJobResult
        from hippo.sync_types import SyncReport
        from hippo.sync_worker import run_worker_once

        async with self.conn.transaction():
            original = await self.jobs.create_job(trigger_type='backfill', biz_list=['one'], source='weixin')
        storage = SimpleNamespace(
            sync_jobs=self.jobs,
            accounts=AccountRepository(self.conn),
            transaction=lambda: StorageTransaction(self.conn),
            rollback=self.conn.rollback,
        )
        saved = asyncio.Event()

        async def interrupted(storage, **kwargs):
            async with storage.transaction():
                await storage.accounts.set_backfill_cursor('one', 'next-page')
            saved.set()
            await asyncio.Event().wait()

        async def poll(*args):
            await asyncio.Event().wait()

        with (
            patch('hippo.sync_worker._poll_cancel', poll),
            patch('hippo.sync_worker.backfill_account_history', interrupted),
        ):
            task = asyncio.create_task(run_worker_once(storage=storage, worker_id='owner'))
            await asyncio.wait_for(saved.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual('queued', (await self.jobs.get_job(original.task_id)).status)

        async def resumed(storage, **kwargs):
            account = await storage.accounts.get_account('one', fallback_to_default=False)
            self.assertEqual('next-page', account.backfill_cursor)
            return SyncJobResult(
                status={'status': 'success'},
                report=SyncReport(total_saved=0, summary=[], details=[], downloaded=0),
                error=None,
            )

        with (
            patch('hippo.sync_worker._poll_cancel', poll),
            patch('hippo.sync_worker.backfill_account_history', resumed),
        ):
            self.assertTrue(await run_worker_once(storage=storage, worker_id='new-owner'))
        self.assertEqual('success', (await self.jobs.get_job(original.task_id)).status)

    async def test_running_group_scope_survives_subscription_moves(self):
        group = await (
            await self.conn.execute(
                "INSERT INTO account_groups (name,created_at,updated_at) VALUES ('moving',NOW(),NOW()) RETURNING id"
            )
        ).fetchone()
        await self.conn.execute("UPDATE subscription SET group_id=%s WHERE biz='one'", (group[0],))
        async with self.conn.transaction():
            running = await self.jobs.create_job(trigger_type='manual', group_id=group[0])
            claimed = await self.jobs.claim_next_job(worker_id='owner')
            await self.jobs.mark_running(claimed.task_id, worker_id='owner')
        self.assertEqual(running.task_id, claimed.task_id)
        self.assertEqual(('weixin',), claimed.scope_sources)
        await self.conn.execute("UPDATE subscription SET group_id=NULL WHERE biz='one'")
        async with self.conn.transaction():
            await self.jobs.create_job(trigger_type='scheduled', source='weixin')
            self.assertIsNone(await self.jobs.claim_next_job(worker_id='other'))

    async def test_shutdown_during_final_flush_releases_running_job(self):
        from hippo.sync_service import SyncJobResult
        from hippo.sync_types import SyncReport
        from hippo.sync_worker import run_worker_once

        async with self.conn.transaction():
            job = await self.jobs.create_job(trigger_type='backfill', biz_list=['one'], source='weixin')
        storage = SimpleNamespace(
            sync_jobs=self.jobs,
            accounts=AccountRepository(self.conn),
            transaction=lambda: StorageTransaction(self.conn),
            rollback=self.conn.rollback,
        )
        flushing = asyncio.Event()

        async def flush(tracker):
            flushing.set()
            await asyncio.Event().wait()

        async def poll(*args):
            await asyncio.Event().wait()

        result = SyncJobResult(
            status={'status': 'success'},
            report=SyncReport(total_saved=0, summary=[], details=[], downloaded=0),
            error=None,
        )
        with (
            patch('hippo.sync_worker._poll_cancel', poll),
            patch('hippo.sync_worker._WorkerProgressTracker.flush', flush),
            patch('hippo.sync_worker.backfill_account_history', AsyncMock(return_value=result)),
        ):
            task = asyncio.create_task(run_worker_once(storage=storage, worker_id='owner'))
            await asyncio.wait_for(flushing.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual('queued', (await self.jobs.get_job(job.task_id)).status)
