from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hippo.server import run_sync
from hippo.sync_service import SyncJobResult
from hippo.sync_types import SyncReport


class _FakeSyncJobs:
    def __init__(self, queued_state=None) -> None:
        self.created: list[dict] = []
        self.queued_state = queued_state
        self.claimed = False
        self.started: list[tuple[str, str]] = []
        self.finished: list[dict] = []
        self.progress_updates: list[dict] = []

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


class _FakeAccounts:
    def __init__(self, biz_values: list[str] | None = None) -> None:
        self._biz_values = biz_values or []

    def list_accounts(self):
        return [SimpleNamespace(biz=value) for value in self._biz_values]


class _FakeStorage:
    def __init__(self, sync_jobs: _FakeSyncJobs, biz_values: list[str] | None = None) -> None:
        self.sync_jobs = sync_jobs
        self.accounts = _FakeAccounts(biz_values)

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


class SyncJobQueueTest(unittest.TestCase):
    def test_run_sync_queues_job_and_returns_queued_status(self) -> None:
        from hippo.sync_jobs import SyncJobState

        queued_state = SyncJobState(
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
        from hippo.sync_jobs import SyncJobState

        queued_state = SyncJobState(
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
        from hippo.sync_jobs import SyncJobState

        state = SyncJobState(
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
        from hippo.sync_jobs import SyncJobState
        from hippo.sync_worker import run_worker_once

        queued_state = SyncJobState(
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
                    },
                }
            ],
        )


if __name__ == '__main__':
    unittest.main()
