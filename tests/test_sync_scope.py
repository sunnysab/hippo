"""Scoped sync runs: a normal user syncs only their own subscriptions."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import Request

from hippo.api.routers.settings import (
    cancel_sync_task,
    get_sync_task,
    list_sync_tasks,
    run_site_sync,
    run_sync,
)
from hippo.api.schemas.sync import SyncRunRequest, SyncSiteRunRequest, SyncTaskListResponse
from hippo.exceptions import ApiError
from hippo.sync_types import AccountProgress, SyncTaskState


class _Jobs:
    """Sync-job store with the same manual-dedupe rule as the database index."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.states: dict[str, SyncTaskState] = {}
        self._active: dict[int, SyncTaskState] = {}
        self._lock = asyncio.Lock()
        self._seq = 0

    async def create_job(
        self,
        *,
        trigger_type: str,
        group_id: int | None = None,
        biz_list: list[str] | None = None,
        source: str | None = None,
        requester_user_id: int | None = None,
        dedupe: bool = False,
    ) -> SyncTaskState:
        async with self._lock:
            if dedupe and requester_user_id is not None and requester_user_id in self._active:
                return self._active[requester_user_id]
            self._seq += 1
            state = SyncTaskState(
                task_id=f'job-{self._seq}',
                status='queued',
                created_at='2026-03-20T04:00:00+00:00',
                group_id=group_id,
                biz_list=tuple(biz_list) if biz_list else None,
                source=source,
                requester_user_id=requester_user_id,
            )
            self.created.append(
                {
                    'trigger_type': trigger_type,
                    'group_id': group_id,
                    'biz_list': biz_list,
                    'source': source,
                    'requester_user_id': requester_user_id,
                    'dedupe': dedupe,
                }
            )
            self.states[state.task_id] = state
            if dedupe and requester_user_id is not None:
                self._active[requester_user_id] = state
            return state

    async def list_jobs(
        self,
        *,
        limit: int = 5,
        source: str | None = None,
        requester_user_id: int | None = None,
    ) -> list[SyncTaskState]:
        rows = [
            state
            for state in self.states.values()
            if requester_user_id is None or state.requester_user_id == requester_user_id
        ]
        if source:
            rows = [state for state in rows if state.source == source]
        return rows[:limit]

    async def get_job(self, task_id: str) -> SyncTaskState | None:
        return self.states.get(task_id)

    async def cancel_job(self, task_id: str) -> bool:
        state = self.states.get(task_id)
        if state is None or state.status != 'queued':
            return False
        state.status = 'cancelled'
        return True


class _Accounts:
    def __init__(self, accounts: list[SimpleNamespace]) -> None:
        self._accounts = accounts

    async def list_followed_accounts(self, user_id: int, **_kwargs) -> list[SimpleNamespace]:
        return list(self._accounts)


class _Storage:
    def __init__(self, jobs: _Jobs, accounts: list[SimpleNamespace]) -> None:
        self.sync_jobs = jobs
        self.accounts = _Accounts(accounts)

    def transaction(self) -> _Storage:
        return self

    async def __aenter__(self) -> _Storage:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def _account(
    biz: str,
    *,
    source: str = 'weixin',
    disabled: bool = False,
    group_id: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(biz=biz, is_disabled=disabled, source=source, group_id=group_id)


def _admin() -> SimpleNamespace:
    return SimpleNamespace(id=1, username='admin', role='admin')


def _user(user_id: int = 7) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, username='reader', role='user')


class SyncRunScopeTest(unittest.TestCase):
    def test_a_normal_user_empty_body_syncs_only_their_enabled_subscriptions(self) -> None:
        jobs = _Jobs()
        storage = _Storage(jobs, [_account('a'), _account('b'), _account('c', disabled=True)])

        result = asyncio.run(run_sync(body=SyncRunRequest(), storage=storage, user=_user(7)))

        self.assertEqual(result.scope, 'own')
        self.assertEqual(result.biz_list, ['a', 'b'])
        self.assertEqual(len(jobs.created), 1)
        self.assertEqual(jobs.created[0]['requester_user_id'], 7)

    def test_a_normal_user_source_is_intersected_with_their_subscriptions(self) -> None:
        storage = _Storage(_Jobs(), [_account('wx', source='weixin'), _account('yt', source='youtube')])

        result = asyncio.run(run_sync(body=SyncRunRequest(source='youtube'), storage=storage, user=_user(7)))

        self.assertEqual(result.biz_list, ['yt'])

    def test_group_and_source_intersect(self) -> None:
        storage = _Storage(
            _Jobs(),
            [
                _account('a', source='weixin', group_id=3),
                _account('b', source='youtube', group_id=3),
                _account('c', source='weixin', group_id=4),
            ],
        )

        with patch('hippo.api.routers.settings.fetchone_row', new=AsyncMock(return_value={'id': 3})):
            result = asyncio.run(
                run_sync(body=SyncRunRequest(group_id=3, source='weixin'), storage=storage, user=_user(7))
            )

        self.assertEqual(result.biz_list, ['a'])

    def test_no_subscription_is_a_404_not_a_whole_site_run(self) -> None:
        jobs = _Jobs()
        storage = _Storage(jobs, [])

        with self.assertRaises(ApiError) as caught:
            asyncio.run(run_sync(body=SyncRunRequest(), storage=storage, user=_user(7)))

        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(jobs.created, [])

    def test_a_source_without_a_subscription_is_a_404(self) -> None:
        jobs = _Jobs()
        storage = _Storage(jobs, [_account('wx', source='weixin')])

        with self.assertRaises(ApiError) as caught:
            asyncio.run(run_sync(body=SyncRunRequest(source='youtube'), storage=storage, user=_user(7)))

        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(jobs.created, [])

    def test_an_empty_biz_list_is_rejected(self) -> None:
        jobs = _Jobs()
        storage = _Storage(jobs, [_account('a')])

        with self.assertRaises(ApiError) as caught:
            asyncio.run(run_sync(body=SyncRunRequest(biz_list=[]), storage=storage, user=_user(7)))

        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(jobs.created, [])

    def test_an_unknown_account_is_a_404(self) -> None:
        jobs = _Jobs()
        storage = _Storage(jobs, [_account('a')])

        with self.assertRaises(ApiError) as caught:
            asyncio.run(run_sync(body=SyncRunRequest(biz_list=['nope']), storage=storage, user=_user(7)))

        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(jobs.created, [])

    def test_run_sync_is_requester_scoped_even_for_an_admin(self) -> None:
        jobs = _Jobs()
        storage = _Storage(jobs, [_account('a')])

        result = asyncio.run(run_sync(body=SyncRunRequest(), storage=storage, user=_admin()))

        # The common route never widens to the catalogue, whoever calls it.
        self.assertEqual(result.scope, 'own')
        self.assertEqual(result.biz_list, ['a'])

    def test_the_explicit_site_route_covers_the_whole_catalogue(self) -> None:
        jobs = _Jobs()
        storage = _Storage(jobs, [_account('a')])

        result = asyncio.run(run_site_sync(body=SyncSiteRunRequest(), storage=storage, user=_admin()))

        self.assertEqual(result.scope, 'site')
        self.assertIsNone(result.biz_list)
        self.assertEqual(jobs.created[0]['requester_user_id'], 1)

    def test_a_different_scope_while_active_is_a_conflict_not_a_silent_drop(self) -> None:
        jobs = _Jobs()
        storage = _Storage(jobs, [_account('a', source='youtube'), _account('b', source='weixin')])

        first = asyncio.run(run_sync(body=SyncRunRequest(source='youtube'), storage=storage, user=_user(7)))
        with self.assertRaises(ApiError) as caught:
            asyncio.run(run_sync(body=SyncRunRequest(source='weixin'), storage=storage, user=_user(7)))

        self.assertEqual(first.scope, 'own')
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(jobs.created), 1)

    def test_a_site_run_conflicts_with_an_active_own_run(self) -> None:
        jobs = _Jobs()
        storage = _Storage(jobs, [_account('a')])

        asyncio.run(run_sync(body=SyncRunRequest(), storage=storage, user=_admin()))
        with self.assertRaises(ApiError) as caught:
            asyncio.run(run_site_sync(body=SyncSiteRunRequest(), storage=storage, user=_admin()))

        self.assertEqual(caught.exception.status, 409)

    def test_repeating_the_site_run_echoes_the_real_old_task(self) -> None:
        jobs = _Jobs()
        storage = _Storage(jobs, [_account('a')])

        first = asyncio.run(run_site_sync(body=SyncSiteRunRequest(), storage=storage, user=_admin()))
        jobs.states[first.task_id].status = 'running'
        second = asyncio.run(run_site_sync(body=SyncSiteRunRequest(), storage=storage, user=_admin()))

        self.assertEqual(second.task_id, first.task_id)
        self.assertEqual(second.scope, 'site')
        self.assertEqual(second.status, 'running')
        self.assertEqual(len(jobs.created), 1)

    def test_unknown_request_keys_are_rejected(self) -> None:
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            SyncRunRequest(source='youtube', scope='site')

    def test_concurrent_manual_runs_queue_only_once(self) -> None:
        jobs = _Jobs()
        storage = _Storage(jobs, [_account('a')])

        async def scenario():
            return await asyncio.gather(
                run_sync(body=SyncRunRequest(), storage=storage, user=_user(7)),
                run_sync(body=SyncRunRequest(), storage=storage, user=_user(7)),
            )

        first, second = asyncio.run(scenario())

        self.assertEqual(len(jobs.created), 1)
        self.assertEqual(first.task_id, second.task_id)


class SyncTaskAccessTest(unittest.TestCase):
    def _request(self) -> Request:
        return Request({'type': 'http'})

    def _two_tasks(self) -> _Jobs:
        jobs = _Jobs()
        jobs.states['mine'] = SyncTaskState(task_id='mine', status='running', created_at='x', requester_user_id=7)
        jobs.states['theirs'] = SyncTaskState(task_id='theirs', status='running', created_at='x', requester_user_id=8)
        return jobs

    def test_a_user_only_lists_their_own_tasks(self) -> None:
        storage = _Storage(self._two_tasks(), [])

        payload = asyncio.run(
            list_sync_tasks(
                request=self._request(), limit=10, detail=False, source=None, storage=storage, user=_user(7)
            )
        )

        self.assertIsInstance(payload, SyncTaskListResponse)
        self.assertEqual([task.task_id for task in payload.tasks], ['mine'])

    def test_an_admin_lists_every_task(self) -> None:
        storage = _Storage(self._two_tasks(), [])

        payload = asyncio.run(
            list_sync_tasks(
                request=self._request(), limit=10, detail=False, source=None, storage=storage, user=_admin()
            )
        )

        self.assertEqual({task.task_id for task in payload.tasks}, {'mine', 'theirs'})

    def test_detail_false_keeps_the_summary_shape(self) -> None:
        storage = _Storage(self._two_tasks(), [])

        payload = asyncio.run(
            list_sync_tasks(
                request=self._request(), limit=10, detail=False, source=None, storage=storage, user=_user(7)
            )
        )
        dumped = payload.model_dump(exclude_unset=True)['tasks'][0]

        self.assertEqual(dumped['task_id'], 'mine')
        # Fields the summary dict never carried stay out of the response.
        self.assertNotIn('accounts', dumped)
        self.assertNotIn('report', dumped)
        self.assertIn('status', dumped)

    def test_detail_true_keeps_the_full_shape(self) -> None:
        jobs = self._two_tasks()
        state = jobs.states['mine']
        state.accounts['biz'] = AccountProgress(biz='biz', nickname='Biz')
        storage = _Storage(jobs, [])

        payload = asyncio.run(
            list_sync_tasks(request=self._request(), limit=10, detail=True, source=None, storage=storage, user=_user(7))
        )
        dumped = payload.model_dump(exclude_unset=True)['tasks'][0]

        self.assertEqual(dumped['accounts'][0]['biz'], 'biz')

    def test_reading_a_foreign_task_is_a_404(self) -> None:
        storage = _Storage(self._two_tasks(), [])

        with self.assertRaises(ApiError) as caught:
            asyncio.run(get_sync_task(task_id='theirs', request=self._request(), storage=storage, user=_user(7)))

        self.assertEqual(caught.exception.status, 404)

    def test_cancelling_a_foreign_task_is_a_404(self) -> None:
        jobs = self._two_tasks()
        jobs.states['theirs'].status = 'queued'
        storage = _Storage(jobs, [])

        with self.assertRaises(ApiError) as caught:
            asyncio.run(cancel_sync_task(task_id='theirs', request=self._request(), storage=storage, user=_user(7)))

        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(jobs.states['theirs'].status, 'queued')

    def test_a_user_can_cancel_their_own_task(self) -> None:
        jobs = _Jobs()
        jobs.states['mine'] = SyncTaskState(task_id='mine', status='queued', created_at='x', requester_user_id=7)
        storage = _Storage(jobs, [])

        payload = asyncio.run(cancel_sync_task(task_id='mine', request=self._request(), storage=storage, user=_user(7)))

        self.assertEqual(payload.status, 'cancelled')


class SyncJobSchemaTest(unittest.TestCase):
    def test_requester_column_and_manual_dedupe_index_are_declared(self) -> None:
        from pathlib import Path

        schema = (Path(__file__).resolve().parent.parent / 'schema' / 'postgres.sql').read_text(encoding='utf-8')

        self.assertIn('ALTER TABLE sync_jobs ADD COLUMN IF NOT EXISTS requester_user_id', schema)
        self.assertIn('CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_jobs_active_manual_requester', schema)


if __name__ == '__main__':
    unittest.main()
