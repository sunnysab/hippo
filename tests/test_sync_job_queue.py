from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from hippo.api.routers.settings import run_sync
from hippo.api.sync_models import SyncRunRequest
from hippo.sync_service import SyncJobResult
from hippo.sync_settings import _persist_sync_outcome
from hippo.sync_types import SyncReport


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
        # Mirrors the partial unique index: one active manual job per requester.
        self._active_manual: dict[int, object] = {}
        self._lock = asyncio.Lock()

    async def create_job(
        self,
        *,
        trigger_type: str,
        group_id: int | None = None,
        biz_list: list[str] | None = None,
        source: str | None = None,
        requester_user_id: int | None = None,
        dedupe: bool = False,
    ):
        async with self._lock:
            if dedupe and requester_user_id is not None and requester_user_id in self._active_manual:
                return self._active_manual[requester_user_id]
            payload = {
                'trigger_type': trigger_type,
                'group_id': group_id,
                'biz_list': biz_list,
                'source': source,
                'requester_user_id': requester_user_id,
                'dedupe': dedupe,
            }
            self.created.append(payload)
            from hippo.sync_types import SyncTaskState

            state = SyncTaskState(
                task_id=f'job-{len(self.created)}',
                status='queued',
                created_at='2026-03-20T04:00:00+00:00',
                group_id=group_id,
                biz_list=tuple(biz_list) if biz_list else None,
                source=source,
                requester_user_id=requester_user_id,
            )
            if dedupe and requester_user_id is not None:
                self._active_manual[requester_user_id] = state
            return state

    async def claim_next_job(self, *, worker_id: str):
        if self.claimed:
            return None
        self.claimed = True
        return self.queued_state

    async def has_active_job(self, *, trigger_type: str | None = None) -> bool:
        return self.active_job

    async def is_cancelling(self, task_id: str) -> bool:
        return False

    async def mark_running(self, task_id: str, *, worker_id: str) -> None:
        self.started.append((task_id, worker_id))

    async def update_progress(
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

    async def mark_finished(
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

    async def recover_stale_running_jobs(self, *, stale_after_minutes: int = 15) -> int:
        self.recovered.append(stale_after_minutes)
        self.active_job = False
        return 1


class _FakeMeta:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    async def get(self, key: str) -> str | None:
        return self._values.get(key)

    async def set(self, key: str, value: str) -> None:
        self._values[key] = value

    async def delete(self, key: str) -> None:
        self._values.pop(key, None)


class _FakeSessions:
    def __init__(self, updated_at: datetime | None = None) -> None:
        self._updated_at = updated_at

    async def get_login_updated_at(self) -> datetime | None:
        return self._updated_at


class _FakeAccounts:
    def __init__(
        self,
        biz_values: list[str] | None = None,
        *,
        disabled: list[str] | None = None,
        sources: dict[str, str] | None = None,
    ) -> None:
        self._biz_values = biz_values or []
        self._disabled = set(disabled or [])
        self._sources = sources or {}

    async def list_accounts(self):
        return [SimpleNamespace(biz=value) for value in self._biz_values]

    async def list_followed_accounts(self, user_id: int, **_kwargs):
        return [
            SimpleNamespace(
                biz=value,
                is_disabled=value in self._disabled,
                source=self._sources.get(value, 'weixin'),
                group_id=None,
            )
            for value in self._biz_values
        ]


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

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _PersistMeta(_FakeMeta):
    pass


class _PersistStorage(_FakeStorage):
    def __init__(self, meta_values: dict[str, str] | None = None) -> None:
        super().__init__(_FakeSyncJobs(), meta_values=meta_values)
        self.sent_emails: list[dict] = []


def _admin() -> SimpleNamespace:
    """Stand-in for the signed-in user (the router receives one via Depends)."""
    return SimpleNamespace(id=1, username='admin', role='admin')


def _user(user_id: int = 7) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, username='reader', role='user')


class SyncJobQueueTest(unittest.TestCase):
    def test_run_sync_queues_job_and_returns_queued_status(self) -> None:
        storage = _FakeStorage(_FakeSyncJobs(), ['biz-a'])

        payload = asyncio.run(run_sync(body=SyncRunRequest(), storage=storage, user=_admin()))

        self.assertEqual(payload.status, 'queued')
        self.assertEqual(payload.task_id, 'job-1')
        # The requester-scoped route resolves "all of mine", never the catalogue.
        self.assertEqual(payload.scope, 'own')
        self.assertEqual(payload.biz_list, ['biz-a'])
        self.assertEqual(
            storage.sync_jobs.created,
            [
                {
                    'trigger_type': 'manual',
                    'group_id': None,
                    'biz_list': ['biz-a'],
                    'source': None,
                    'requester_user_id': 1,
                    'dedupe': True,
                }
            ],
        )

    def test_run_sync_normalizes_biz_list_before_queueing(self) -> None:
        storage = _FakeStorage(_FakeSyncJobs(), ['biz-a', 'biz-b'])

        payload = asyncio.run(
            run_sync(
                body=SyncRunRequest(biz_list=['biz-a', ' ', 'biz-b', 'biz-a']),
                storage=storage,
                user=_admin(),
            )
        )

        self.assertEqual(payload.status, 'queued')
        self.assertEqual(payload.biz_list, ['biz-a', 'biz-b'])
        self.assertEqual(
            storage.sync_jobs.created,
            [
                {
                    'trigger_type': 'manual',
                    'group_id': None,
                    'biz_list': ['biz-a', 'biz-b'],
                    'source': None,
                    'requester_user_id': 1,
                    'dedupe': True,
                }
            ],
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
                'source': None,
                'phase': 'content',
                'accounts_total': 2,
                'accounts_done': 1,
                'current_account': {'biz': 'demo', 'nickname': 'Demo'},
                'current_article': {'article_id': '100-1', 'title': 'Story'},
                'last_log': 'downloading',
                'requester_user_id': None,
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
        self.assertEqual(len(storage.sync_jobs.finished), 1)
        finished = storage.sync_jobs.finished[0]
        self.assertEqual(finished['task_id'], 'job-4')
        self.assertEqual(finished['status'], 'success')
        self.assertIsNone(finished['error'])
        report = finished['result']
        self.assertEqual(report['total_saved'], 5)
        self.assertEqual(report['downloaded'], 3)
        self.assertEqual(report['summary'], [('Demo', 5)])
        self.assertEqual(report['failed_accounts'], 0)
        # 结束前的进度行就先带上整份报告，管理页不用等任务结束才看得到明细
        self.assertEqual(storage.sync_jobs.progress_updates[-1]['report']['total_saved'], 5)

    def test_sync_job_repository_uses_statement_timestamp_for_running_and_finished(self) -> None:
        from hippo.sync_jobs import SyncJobRepository

        class _Cursor:
            def __init__(self, queries: list[str]) -> None:
                self._queries = queries

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def execute(self, query: str, params=None) -> None:
                self._queries.append(query)

        class _Conn:
            def __init__(self) -> None:
                self.queries: list[str] = []

            def cursor(self, **kwargs):
                return _Cursor(self.queries)

        conn = _Conn()
        repo = SyncJobRepository(conn)

        asyncio.run(repo.mark_running('job-5', worker_id='worker-1'))
        asyncio.run(repo.mark_finished('job-5', status='failed', error='boom', result=None))

        joined = '\n'.join(conn.queries)
        self.assertIn('clock_timestamp()', joined)

    def test_sync_job_repository_recovers_stale_running_jobs(self) -> None:
        from hippo.sync_jobs import SyncJobRepository

        class _Cursor:
            def __init__(self, queries: list[str]) -> None:
                self._queries = queries
                self.rowcount = 3

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def execute(self, query: str, params=None) -> None:
                self._queries.append(query)

        class _Conn:
            def __init__(self) -> None:
                self.queries: list[str] = []

            def cursor(self, **kwargs):
                return _Cursor(self.queries)

        conn = _Conn()
        repo = SyncJobRepository(conn)

        recovered = asyncio.run(repo.recover_stale_running_jobs())

        self.assertEqual(recovered, 3)
        joined = '\n'.join(conn.queries)
        # Both active statuses are recovered; a stale cancellation releases the
        # manual unique index by landing in ``cancelled``, a dead run in ``failed``.
        self.assertIn("WHERE status IN ('running', 'cancelling')", joined)
        self.assertIn("locked_at < NOW() - (%s * INTERVAL '1 minute')", joined)
        self.assertIn("WHEN 'cancelling' THEN 'cancelled'", joined)
        self.assertIn("ELSE 'failed'", joined)

    def test_worker_recovers_stale_running_jobs_before_processing(self) -> None:
        from hippo.sync_worker import recover_stale_running_jobs

        sync_jobs = _FakeSyncJobs()
        storage = _FakeStorage(sync_jobs)

        recovered = asyncio.run(recover_stale_running_jobs(storage))

        self.assertEqual(recovered, 1)
        self.assertEqual(sync_jobs.recovered, [15])

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
        asyncio.run(tracker._save())

        update = storage.sync_jobs.progress_updates[-1]
        self.assertEqual(update['accounts_done'], 1)
        self.assertEqual(update['accounts'][0]['status'], 'failed')
        self.assertEqual(update['accounts'][0]['error'], 'invalid args')

    def test_tracker_events_persist_even_though_nobody_awaits_them(self) -> None:
        # 观察者协议是同步的，调用点不会 await：这些方法以前是 async def，于是协程从不
        # 执行、进度永远停在 0%。现在它们同步改状态、自己把写入排出去。
        from hippo.sync_worker import _WorkerProgressTracker

        async def scenario() -> str:
            storage = _FakeStorage(_FakeSyncJobs())
            tracker = _WorkerProgressTracker(storage=storage, task_id='job-7')
            tracker.on_log('正文阶段：处理待抓队列')
            await tracker._persist()
            return storage.sync_jobs.progress_updates[-1]['last_log']

        self.assertEqual('正文阶段：处理待抓队列', asyncio.run(scenario()))

    def test_the_tracker_coalesces_writes(self) -> None:
        from hippo.sync_worker import _WorkerProgressTracker

        async def scenario() -> tuple[object, object, int]:
            storage = _FakeStorage(_FakeSyncJobs())
            tracker = _WorkerProgressTracker(storage=storage, task_id='job-8')
            tracker.on_log('一条')
            first = tracker._persist()
            tracker.on_log('两条')
            second = tracker._persist()
            await first
            return (first, second, len(storage.sync_jobs.progress_updates))

        first, second, writes = asyncio.run(scenario())
        # 上一次还没写完时复用同一次写入（状态累计，丢一次没有信息损失）。
        self.assertIs(first, second)
        self.assertEqual(1, writes)

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

        with patch('hippo.sync_settings._send_sync_alert') as mock_alert:
            status = asyncio.run(
                _persist_sync_outcome(
                    storage,
                    started_at='2026-04-03T11:30:54.694914+00:00',
                    finished_at='2026-04-03T11:37:43.925005+00:00',
                    error=None,
                    report=report,
                )
            )

        self.assertEqual(status['status'], 'success')
        self.assertEqual(asyncio.run(storage.meta.get('sync:last_error')), '')
        self.assertIn('failed_accounts', asyncio.run(storage.meta.get('sync:history')) or '')
        mock_alert.assert_called_once()

    def test_manual_job_creation_dedupes_on_the_unique_index(self) -> None:
        from hippo.sync_jobs import SyncJobRepository

        class _Cursor:
            def __init__(self) -> None:
                self.queries: list[str] = []
                self.results: list[dict | None] = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def execute(self, query: str, params=None) -> None:
                self.queries.append(query)

            async def fetchone(self):
                return self.results.pop(0) if self.results else None

        class _Conn:
            def __init__(self, cursor: _Cursor) -> None:
                self.cursor_obj = cursor

            def cursor(self, **kwargs):
                return self.cursor_obj

        cursor = _Cursor()
        cursor.results = [
            None,  # the partial unique index rejected the insert
            {
                'id': 'existing',
                'status': 'running',
                'created_at': datetime(2026, 3, 20, tzinfo=UTC),
                'requester_user_id': 7,
            },
        ]
        repo = SyncJobRepository(_Conn(cursor))

        state = asyncio.run(repo.create_job(trigger_type='manual', requester_user_id=7, dedupe=True))

        self.assertEqual(state.task_id, 'existing')
        joined = '\n'.join(cursor.queries)
        self.assertIn('ON CONFLICT DO NOTHING', joined)
        self.assertIn("status IN ('queued', 'running', 'cancelling')", joined)
        self.assertIn('requester_user_id = %s', joined)

    def test_worker_forces_manual_jobs_but_not_scheduled_ones(self) -> None:
        from hippo.sync_types import SyncTaskState
        from hippo.sync_worker import run_worker_once

        result = SyncJobResult(
            status={'status': 'success'},
            report=SyncReport(total_saved=0, summary=[], details=[]),
            error=None,
        )
        for trigger_type, expected in (('manual', True), ('scheduled', False), ('subscription', False)):
            queued_state = SyncTaskState(
                task_id=f'job-{trigger_type}',
                status='queued',
                created_at='2026-03-20T04:00:00+00:00',
                trigger_type=trigger_type,
            )
            storage = _FakeStorage(_FakeSyncJobs(queued_state))

            with patch('hippo.sync_worker.run_sync_job', return_value=result) as mocked:
                asyncio.run(run_worker_once(storage=storage, worker_id='worker-1'))

            self.assertEqual(mocked.call_args.kwargs['force'], expected)
