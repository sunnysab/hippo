from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.server import run_sync
from hippo.sync_service import ArticleSyncService, SyncJobResult, _persist_sync_outcome
from hippo.sync_types import SyncConfig, SyncMode, SyncReport, SyncSummary


class _FakeSyncJobs:
    def __init__(self, queued_state=None) -> None:
        self.created: list[dict] = []
        self.queued_state = queued_state
        self.claimed = False
        self.started: list[tuple[str, str]] = []
        self.finished: list[dict] = []
        self.progress_updates: list[dict] = []
        self.active_job = False
        self.recovered: list[int] = []

    def create_job(
        self,
        *,
        trigger_type: str,
        group_id: int | None = None,
        biz_list: list[str] | None = None,
    ):
        payload = {
            'trigger_type': trigger_type,
            'group_id': group_id,
            'biz_list': biz_list,
        }
        self.created.append(payload)
        return self.queued_state

    def claim_next_job(self, *, worker_id: str):
        if self.claimed:
            return None
        self.claimed = True
        return self.queued_state

    def has_active_job(self, *, trigger_type: str | None = None) -> bool:
        return self.active_job

    def mark_running(self, task_id: str, *, worker_id: str) -> None:
        self.started.append((task_id, worker_id))

    def update_progress(
        self,
        task_id: str,
        *,
        phase,
        accounts_total,
        accounts_done,
        current_account,
        current_article,
        last_log,
        accounts,
        report=None,
    ) -> None:
        self.progress_updates.append(
            {
                'task_id': task_id,
                'phase': phase,
                'accounts_total': accounts_total,
                'accounts_done': accounts_done,
                'current_account': current_account,
                'current_article': current_article,
                'last_log': last_log,
                'accounts': accounts,
                'report': report,
            }
        )

    def mark_finished(
        self,
        task_id: str,
        *,
        status: str,
        error: str | None,
        result: dict | None,
    ) -> None:
        self.finished.append(
            {
                'task_id': task_id,
                'status': status,
                'error': error,
                'result': result,
            }
        )

    def recover_stale_running_jobs(self, *, stale_after_minutes: int = 15) -> int:
        self.recovered.append(stale_after_minutes)
        self.active_job = False
        return 1


class _FakeMeta:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def get(self, key: str) -> str | None:
        return self._values.get(key)

    def set(self, key: str, value: str) -> None:
        self._values[key] = value

    def delete(self, key: str) -> None:
        self._values.pop(key, None)


class _FakeSessions:
    def __init__(self, updated_at: datetime | None = None) -> None:
        self._updated_at = updated_at

    def get_login_updated_at(self) -> datetime | None:
        return self._updated_at


class _FakeAccounts:
    def __init__(self, biz_values: list[str] | None = None) -> None:
        self._biz_values = biz_values or []

    def list_accounts(self):
        return [SimpleNamespace(biz=value) for value in self._biz_values]


class _FakeStorage:
    def __init__(
        self,
        sync_jobs: _FakeSyncJobs,
        biz_values: list[str] | None = None,
        *,
        meta_values: dict[str, str] | None = None,
        login_updated_at: datetime | None = None,
    ) -> None:
        self.sync_jobs = sync_jobs
        self.accounts = _FakeAccounts(biz_values)
        self.meta = _FakeMeta(meta_values)
        self.sessions = _FakeSessions(login_updated_at)

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


class _PersistMeta(_FakeMeta):
    pass


class _PersistStorage(_FakeStorage):
    def __init__(self, meta_values: dict[str, str] | None = None) -> None:
        super().__init__(_FakeSyncJobs(), meta_values=meta_values)
        self.sent_emails: list[dict] = []


class SyncJobQueueTest(unittest.TestCase):
    def test_invalid_args_is_not_treated_as_login_error(self) -> None:
        from hippo.sync_core import is_login_error

        self.assertFalse(is_login_error('invalid args'))
        self.assertTrue(is_login_error('invalid token'))
        self.assertTrue(is_login_error('session expired'))

    def test_stale_login_required_state_from_invalid_args_is_cleared(self) -> None:
        from hippo.sync_service import _should_skip_for_login

        storage = _FakeStorage(
            _FakeSyncJobs(),
            meta_values={
                'sync:last_status': 'login_required',
                'sync:last_error': 'invalid args',
                'sync:login_required_at': '2026-04-03T11:37:43.925005+00:00',
                'sync:alert_sent': '1',
            },
        )

        blocked = _should_skip_for_login(storage)

        self.assertFalse(blocked)
        self.assertIsNone(storage.meta.get('sync:login_required_at'))
        self.assertIsNone(storage.meta.get('sync:alert_sent'))
        self.assertEqual(storage.meta.get('sync:last_status'), 'failed')

    def test_scheduler_does_not_enqueue_when_login_is_still_required(self) -> None:
        from hippo.sync_worker import maybe_enqueue_scheduled_job

        storage = _FakeStorage(
            _FakeSyncJobs(),
            meta_values={
                'sync:last_status': 'login_required',
                'sync:last_error': 'session expired',
                'sync:login_required_at': '2026-04-03T11:37:43.925005+00:00',
            },
        )

        with (
            patch(
                'hippo.sync_worker.get_sync_settings',
                return_value={
                    'enabled': True,
                    'interval_minutes': 60,
                    'window_start_hour': 0,
                    'window_end_hour': 24,
                },
            ),
            patch('hippo.sync_worker._is_within_sync_window', return_value=True),
        ):
            created = maybe_enqueue_scheduled_job(storage)

        self.assertFalse(created)
        self.assertEqual(storage.sync_jobs.created, [])

    def test_run_sync_queues_job_and_returns_queued_status(self) -> None:
        from hippo.sync_types import SyncTaskState

        queued_state = SyncTaskState(
            task_id='job-1',
            status='queued',
            created_at='2026-03-20T04:00:00+00:00',
        )
        storage = _FakeStorage(_FakeSyncJobs(queued_state))

        payload = asyncio.run(run_sync(body={}, storage=storage))

        self.assertEqual(payload, {'status': 'queued', 'task_id': 'job-1'})
        self.assertEqual(
            storage.sync_jobs.created,
            [{'trigger_type': 'manual', 'group_id': None, 'biz_list': None}],
        )

    def test_run_sync_normalizes_biz_list_before_queueing(self) -> None:
        from hippo.sync_types import SyncTaskState

        queued_state = SyncTaskState(
            task_id='job-2',
            status='queued',
            created_at='2026-03-20T04:00:00+00:00',
            biz_list=('biz-a', 'biz-b'),
        )
        storage = _FakeStorage(_FakeSyncJobs(queued_state), ['biz-a', 'biz-b'])

        payload = asyncio.run(
            run_sync(
                body={'biz_list': ['biz-a', ' ', 'biz-b', 'biz-a']},
                storage=storage,
            )
        )

        self.assertEqual(payload['status'], 'queued')
        self.assertEqual(payload['biz_list'], ['biz-a', 'biz-b'])
        self.assertEqual(
            storage.sync_jobs.created,
            [{'trigger_type': 'manual', 'group_id': None, 'biz_list': ['biz-a', 'biz-b']}],
        )

    def test_sync_job_state_serializes_like_legacy_task_shape(self) -> None:
        from hippo.sync_types import SyncTaskState

        state = SyncTaskState(
            task_id='job-3',
            status='running',
            created_at='2026-03-20T04:00:00+00:00',
            started_at='2026-03-20T04:00:03+00:00',
            group_id=7,
            phase='content',
            accounts_total=2,
            accounts_done=1,
            current_account={'biz': 'demo', 'nickname': 'Demo'},
            current_article={'article_id': '100-1', 'title': 'Story'},
            last_log='downloading',
        )

        self.assertEqual(
            state.to_summary_dict(),
            {
                'task_id': 'job-3',
                'status': 'running',
                'created_at': '2026-03-20T04:00:00+00:00',
                'started_at': '2026-03-20T04:00:03+00:00',
                'finished_at': None,
                'error': None,
                'group_id': 7,
                'phase': 'content',
                'accounts_total': 2,
                'accounts_done': 1,
                'current_account': {'biz': 'demo', 'nickname': 'Demo'},
                'current_article': {'article_id': '100-1', 'title': 'Story'},
                'last_log': 'downloading',
            },
        )

    def test_worker_claims_and_finishes_queued_job(self) -> None:
        from hippo.sync_types import SyncTaskState
        from hippo.sync_worker import run_worker_once

        queued_state = SyncTaskState(
            task_id='job-4',
            status='queued',
            created_at='2026-03-20T04:00:00+00:00',
        )
        storage = _FakeStorage(_FakeSyncJobs(queued_state))
        result = SyncJobResult(
            status={'status': 'success'},
            report=SyncReport(total_saved=5, summary=[('Demo', 5)], details=[], downloaded=3),
            error=None,
        )

        with patch('hippo.sync_worker.run_sync_job', return_value=result):
            handled = asyncio.run(run_worker_once(storage=storage, worker_id='worker-1'))

        self.assertTrue(handled)
        self.assertEqual(storage.sync_jobs.started, [('job-4', 'worker-1')])
        self.assertEqual(
            storage.sync_jobs.finished,
            [
                {
                    'task_id': 'job-4',
                    'status': 'success',
                    'error': None,
                    'result': {
                        'total_saved': 5,
                        'downloaded': 3,
                        'summary': [('Demo', 5)],
                        'failed_accounts': 0,
                    },
                }
            ],
        )

    def test_sync_job_repository_uses_statement_timestamp_for_running_and_finished(self) -> None:
        from hippo.sync_jobs import SyncJobRepository

        class _Cursor:
            def __init__(self, queries: list[str]) -> None:
                self._queries = queries

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def execute(self, query: str, params=None) -> None:
                self._queries.append(query)

        class _Conn:
            def __init__(self) -> None:
                self.queries: list[str] = []

            def cursor(self, **kwargs):
                return _Cursor(self.queries)

        conn = _Conn()
        repo = SyncJobRepository(conn)

        repo.mark_running('job-5', worker_id='worker-1')
        repo.mark_finished('job-5', status='failed', error='boom', result=None)

        joined = '\n'.join(conn.queries)
        self.assertIn('clock_timestamp()', joined)

    def test_sync_job_repository_recovers_stale_running_jobs(self) -> None:
        from hippo.sync_jobs import SyncJobRepository

        class _Cursor:
            def __init__(self, queries: list[str]) -> None:
                self._queries = queries
                self.rowcount = 3

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def execute(self, query: str, params=None) -> None:
                self._queries.append(query)

        class _Conn:
            def __init__(self) -> None:
                self.queries: list[str] = []

            def cursor(self, **kwargs):
                return _Cursor(self.queries)

        conn = _Conn()
        repo = SyncJobRepository(conn)

        recovered = repo.recover_stale_running_jobs()

        self.assertEqual(recovered, 3)
        joined = '\n'.join(conn.queries)
        self.assertIn("WHERE status = 'running'", joined)
        self.assertIn("locked_at < NOW() - (%s * INTERVAL '1 minute')", joined)
        self.assertIn("status = 'failed'", joined)

    def test_worker_recovers_stale_running_jobs_before_processing(self) -> None:
        from hippo.sync_worker import recover_stale_running_jobs

        sync_jobs = _FakeSyncJobs()
        storage = _FakeStorage(sync_jobs)

        recovered = recover_stale_running_jobs(storage)

        self.assertEqual(recovered, 1)
        self.assertEqual(sync_jobs.recovered, [15])

    def test_bulk_sync_continues_after_non_login_account_failure(self) -> None:
        config = SyncConfig(
            mode=SyncMode.full,
            page_size=5,
            sleep_seconds=0,
            reset=False,
            recent_days=None,
            since_date=None,
            until_date=None,
            force=False,
            skip_minutes=None,
            download_content=False,
            download_images=False,
            content_limit=None,
        )
        accounts = [
            SimpleNamespace(biz='biz-1', nickname='First'),
            SimpleNamespace(biz='biz-2', nickname='Second'),
        ]
        service = ArticleSyncService(storage=SimpleNamespace(), client=SimpleNamespace())
        service.sync_account = AsyncMock(
            side_effect=[
                (
                    SimpleNamespace(
                        biz='biz-1',
                        nickname='First',
                        saved=0,
                        completed=False,
                        skipped=False,
                        skip_reason=None,
                        failed=True,
                        error='invalid args',
                    ),
                    [],
                    None,
                ),
                (
                    SimpleNamespace(
                        biz='biz-2',
                        nickname='Second',
                        saved=3,
                        completed=True,
                        skipped=False,
                        skip_reason=None,
                        failed=False,
                        error=None,
                    ),
                    [],
                    SyncSummary(total_saved=3, page_count=1, completed=True),
                ),
            ]
        )

        report = asyncio.run(
            service.sync_accounts(
                accounts=accounts,
                config=config,
                bulk=True,
                use_resume=False,
            )
        )

        self.assertEqual(report.total_saved, 3)
        self.assertEqual(report.failed_accounts, 1)
        self.assertEqual(report.summary, [('Second', 3)])
        self.assertEqual(len(report.details), 2)
        self.assertTrue(report.details[0].failed)
        self.assertEqual(report.details[0].error, 'invalid args')

    def test_bulk_sync_login_required_error_carries_partial_report(self) -> None:
        from hippo.sync_service import SyncRunError

        config = SyncConfig(
            mode=SyncMode.full,
            page_size=5,
            sleep_seconds=0,
            reset=False,
            recent_days=None,
            since_date=None,
            until_date=None,
            force=False,
            skip_minutes=None,
            download_content=False,
            download_images=False,
            content_limit=None,
        )
        accounts = [
            SimpleNamespace(biz='biz-1', nickname='First'),
            SimpleNamespace(biz='biz-2', nickname='Second'),
        ]
        service = ArticleSyncService(storage=SimpleNamespace(), client=SimpleNamespace())
        service.sync_account = AsyncMock(
            side_effect=[
                (
                    SimpleNamespace(
                        biz='biz-1',
                        nickname='First',
                        saved=3,
                        completed=True,
                        skipped=False,
                        skip_reason=None,
                        failed=False,
                        error=None,
                    ),
                    [],
                    SyncSummary(total_saved=3, page_count=1, completed=True),
                ),
                SyncRunError('invalid session', login_required=True),
            ]
        )

        with self.assertRaises(SyncRunError) as ctx:
            asyncio.run(
                service.sync_accounts(
                    accounts=accounts,
                    config=config,
                    bulk=True,
                    use_resume=False,
                )
            )

        self.assertEqual(ctx.exception.report.total_saved, 3)
        self.assertEqual(ctx.exception.report.summary, [('First', 3)])
        self.assertEqual(ctx.exception.report.accounts_done, 1)
        self.assertEqual(ctx.exception.report.accounts_total, 2)
        self.assertEqual(
            ctx.exception.report.current_account,
            {'biz': 'biz-2', 'nickname': 'Second'},
        )

    def test_worker_tracker_marks_failed_account_status(self) -> None:
        from hippo.sync_worker import _WorkerProgressTracker

        storage = _FakeStorage(_FakeSyncJobs())
        tracker = _WorkerProgressTracker(storage=storage, task_id='job-6')
        result = SimpleNamespace(
            biz='biz-1',
            nickname='Broken',
            saved=0,
            completed=False,
            skipped=False,
            skip_reason=None,
            failed=True,
            error='invalid args',
        )

        tracker.on_account_done(result, None)

        update = storage.sync_jobs.progress_updates[-1]
        self.assertEqual(update['accounts_done'], 1)
        self.assertEqual(update['accounts'][0]['status'], 'failed')
        self.assertEqual(update['accounts'][0]['error'], 'invalid args')

    def test_persist_sync_outcome_records_failed_account_count_without_failing_job(self) -> None:
        storage = _FakeStorage(_FakeSyncJobs())
        report = SyncReport(
            total_saved=3,
            summary=[('Second', 3)],
            details=[
                SimpleNamespace(
                    biz='biz-1',
                    nickname='Broken',
                    saved=0,
                    completed=False,
                    skipped=False,
                    skip_reason=None,
                    failed=True,
                    error='invalid args',
                ),
                SimpleNamespace(
                    biz='biz-2',
                    nickname='Second',
                    saved=3,
                    completed=True,
                    skipped=False,
                    skip_reason=None,
                    failed=False,
                    error=None,
                ),
            ],
            downloaded=1,
            failed_accounts=1,
        )

        with patch('hippo.sync_service._send_sync_alert') as mock_alert:
            status = _persist_sync_outcome(
                storage,
                started_at='2026-04-03T11:30:54.694914+00:00',
                finished_at='2026-04-03T11:37:43.925005+00:00',
                error=None,
                report=report,
            )

        self.assertEqual(status['status'], 'success')
        self.assertEqual(storage.meta.get('sync:last_error'), '')
        self.assertIn('failed_accounts', storage.meta.get('sync:history') or '')
        mock_alert.assert_called_once()

    def test_persist_sync_outcome_records_partial_progress_for_login_required(self) -> None:
        storage = _FakeStorage(_FakeSyncJobs())
        report = SyncReport(
            total_saved=12,
            summary=[('First', 7), ('Second', 5)],
            details=[],
            downloaded=10,
            failed_accounts=0,
            accounts_total=237,
            accounts_done=180,
            current_account={'biz': 'biz-181', 'nickname': '烟台大学'},
        )

        with patch('hippo.sync_service._send_sync_alert') as mock_alert:
            status = _persist_sync_outcome(
                storage,
                started_at='2026-05-14T03:00:17.444228+00:00',
                finished_at='2026-05-14T03:11:41.277604+00:00',
                error='invalid session',
                report=report,
            )

        history = storage.meta.get('sync:history') or ''
        self.assertEqual(status['status'], 'login_required')
        self.assertIn('"saved": 12', history)
        self.assertIn('"downloaded": 10', history)
        self.assertIn('"accounts_total": 237', history)
        self.assertIn('"accounts_done": 180', history)
        self.assertIn('烟台大学', history)
        mock_alert.assert_called_once()


if __name__ == '__main__':
    unittest.main()
