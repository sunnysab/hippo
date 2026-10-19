"""An API key may never authenticate an admin route.

The admin dependency branches on the authentication *context*, not the path, so
every route that gates on ``require_admin`` refuses a key even when the key's
owner is an administrator. These cases run the real ``settings`` and ``llm``
routers over ASGI with only the storage stubbed, so the real ``api_key_user``
dependency is what puts ``state.api_key`` on the request.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hippo.api import deps
from hippo.api.errors import install_exception_handlers
from hippo.api.routers import llm as llm_api
from hippo.api.routers import settings as settings_api
from hippo.sync_types import SyncTaskState


class _ApiKeys:
    def __init__(self, key: dict[str, Any] | None) -> None:
        self._key = key
        self.touched: list[tuple[int, str | None]] = []

    async def authenticate(self, secret: str) -> dict[str, Any] | None:
        return self._key

    async def touch(self, key_id: int, ip: str | None) -> None:
        self.touched.append((key_id, ip))


class _Users:
    def __init__(self, user: SimpleNamespace) -> None:
        self._user = user

    async def get(self, user_id: int) -> SimpleNamespace:
        return self._user


class _Sessions:
    def __init__(self, user: SimpleNamespace) -> None:
        self._user = user

    async def resolve(self, token: str | None) -> SimpleNamespace | None:
        return self._user if token else None


class _Accounts:
    async def list_followed_accounts(self, user_id: int) -> list[SimpleNamespace]:
        return [SimpleNamespace(biz='biz-a', is_disabled=False, source='weixin', group_id=None)]


class _SyncJobs:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.states: dict[str, SyncTaskState] = {}

    async def list_jobs(self, *, limit: int, source: str | None, requester_user_id: int | None) -> list[SyncTaskState]:
        return [
            state
            for state in self.states.values()
            if requester_user_id is None or state.requester_user_id == requester_user_id
        ][:limit]

    async def get_job(self, task_id: str) -> SyncTaskState | None:
        return self.states.get(task_id)

    async def cancel_job(self, task_id: str) -> bool:
        state = self.states.get(task_id)
        if state is None:
            return False
        state.status = 'cancelled'
        return True

    async def create_job(self, **kwargs: Any) -> SyncTaskState:
        self.created.append(kwargs)
        return SyncTaskState(
            task_id='job-1',
            status='queued',
            created_at='2026-03-20T04:00:00+00:00',
            group_id=kwargs.get('group_id'),
            biz_list=tuple(kwargs['biz_list']) if kwargs.get('biz_list') else None,
            source=kwargs.get('source'),
            requester_user_id=kwargs.get('requester_user_id'),
        )


class _Llm:
    async def list_all(self) -> list[dict[str, Any]]:
        return []


class _Transaction:
    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Storage:
    def __init__(self, *, api_key: dict[str, Any] | None, user: SimpleNamespace) -> None:
        self.api_keys = _ApiKeys(api_key)
        self.users = _Users(user)
        self.sessions = _Sessions(user)
        self.accounts = _Accounts()
        self.sync_jobs = _SyncJobs()
        self.llm = _Llm()

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def commit(self) -> None:
        return None


def _client(storage: _Storage) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(settings_api.router, prefix='/api')
    app.include_router(llm_api.router, prefix='/api')
    app.dependency_overrides[deps.get_storage] = lambda: storage
    return TestClient(app)


def _admin() -> SimpleNamespace:
    return SimpleNamespace(id=1, username='root', role='admin', is_disabled=False)


#: A write key owned by an admin: full role, but still an API key.
_ADMIN_KEY = {'id': 9, 'user_id': 1, 'name': 'ci', 'scopes': ['write']}


class AdminApiKeyIsolationTest(unittest.TestCase):
    def test_a_write_key_is_refused_by_the_management_surface(self) -> None:
        client = _client(_Storage(api_key=_ADMIN_KEY, user=_admin()))
        headers = {'X-API-Key': 'sk-secret'}

        for method, path, body in (
            ('get', '/api/settings', None),
            ('patch', '/api/settings', {'enabled': True}),
            ('post', '/api/settings/admin/run', {}),
            ('get', '/api/llm/provider', None),
        ):
            with self.subTest(path=path):
                response = client.request(method, path, headers=headers, json=body)
                self.assertEqual(403, response.status_code, response.text)

    def test_a_session_cookie_admin_is_allowed(self) -> None:
        client = _client(_Storage(api_key=None, user=_admin()))
        client.cookies.set(deps.SESSION_COOKIE, 'session-token')

        response = client.get('/api/llm/provider')

        self.assertEqual(200, response.status_code, response.text)

    def test_an_admin_key_cannot_inherit_site_wide_task_access(self) -> None:
        storage = _Storage(api_key=_ADMIN_KEY, user=_admin())
        for task_id, owner in (('mine', 1), ('theirs', 8), ('site', None)):
            storage.sync_jobs.states[task_id] = SyncTaskState(
                task_id=task_id,
                status='queued',
                created_at='2026-03-20T04:00:00+00:00',
                requester_user_id=owner,
            )
        client = _client(storage)
        headers = {'X-API-Key': 'sk-secret'}

        response = client.get('/api/settings/tasks', headers=headers)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(['mine'], [item['task_id'] for item in response.json()['tasks']])
        for task_id in ('theirs', 'site'):
            with self.subTest(task_id=task_id):
                response = client.get(f'/api/settings/tasks/{task_id}', headers=headers)
                self.assertEqual(404, response.status_code, response.text)
                response = client.post(f'/api/settings/tasks/{task_id}/cancel', headers=headers)
                self.assertEqual(404, response.status_code, response.text)
                self.assertEqual('queued', storage.sync_jobs.states[task_id].status)

        response = client.get('/api/settings/tasks/mine', headers=headers)
        self.assertEqual(200, response.status_code, response.text)
        response = client.post('/api/settings/tasks/mine/cancel', headers=headers)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('cancelled', response.json()['status'])

        client.cookies.set(deps.SESSION_COOKIE, 'session-token')
        response = client.get('/api/settings/tasks', headers=headers)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({'mine', 'theirs', 'site'}, {item['task_id'] for item in response.json()['tasks']})
        response = client.post('/api/settings/tasks/theirs/cancel', headers=headers)
        self.assertEqual(200, response.status_code, response.text)

    def test_a_normal_key_still_runs_its_own_subscriptions(self) -> None:
        storage = _Storage(api_key=_ADMIN_KEY, user=_admin())
        client = _client(storage)

        response = client.post('/api/settings/run', headers={'X-API-Key': 'sk-secret'}, json={})

        self.assertEqual(202, response.status_code, response.text)
        self.assertEqual('own', response.json()['scope'])
        self.assertEqual(1, storage.sync_jobs.created[0]['requester_user_id'])


if __name__ == '__main__':
    unittest.main()
