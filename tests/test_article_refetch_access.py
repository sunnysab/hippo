"""Refetch authorization at the shared handlers, not just the UI.

The POST starts a downloader and the GET reads a cached task, so both need the
caller to pass the subscription-visibility gate and the task gate. These tests
exercise the real routes in-process with fake storage, auth, downloader and
thread: they pin identity from the authenticated dependency, generic 404 for a
foreign or unknown task, and no internal owner bookkeeping in the status JSON.
"""

import unittest
from collections.abc import Mapping
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hippo.api import deps
from hippo.api.errors import install_exception_handlers
from hippo.api.routers import article as article_api
from hippo.exceptions import ApiError

ARTICLE_ID = 7


class _DeferredThread:
    """Capture the downloader thread instead of letting it run for real."""

    def __init__(self, target=None, daemon=None, **kwargs) -> None:
        self._target = target

    def start(self) -> None:
        _DeferredThread.targets.append(self._target)


def _user(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, username=f'user{user_id}', role='user')


class _AsyncContext:
    def __init__(self, value) -> None:
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


class RefetchAccessHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        _DeferredThread.targets = []
        with article_api._refetch_lock:
            article_api._refetch_tasks.clear()

    def tearDown(self) -> None:
        with article_api._refetch_lock:
            article_api._refetch_tasks.clear()

    def _client(self, visible: set[tuple[int, int]]) -> TestClient:
        """Build the real routes with auth, storage, thread and download faked.

        The authenticated identity comes only from the ``x-test-user`` header
        (the override), so a ``user_id`` in the query or body cannot reach it.
        """
        app = FastAPI()
        install_exception_handlers(app)

        async def fake_current_user(request: Request):
            raw = request.headers.get('x-test-user')
            if not raw:
                raise ApiError(deps.NOT_SIGNED_IN, status=401, code=deps.SESSION_MISSING)
            return _user(int(raw))

        async def fake_visibility(storage, user_id: int, article_id: int) -> None:
            if (user_id, article_id) not in visible:
                raise ApiError('Article not found', status=404)

        self.downloader = SimpleNamespace(download_from_url=AsyncMock())
        app.dependency_overrides[deps.current_user] = fake_current_user
        app.dependency_overrides[deps.get_storage] = lambda: SimpleNamespace()
        app.include_router(article_api.router, prefix='/api')
        self.visibility_patch = patch.object(article_api, '_assert_article_visible', fake_visibility)
        self.row_patch = patch.object(
            article_api,
            'fetchone_row',
            AsyncMock(
                return_value={'biz': 'Mz123', 'article_id': 'slug-1', 'link': 'https://mp.weixin.qq.com/s/AbCdEf'}
            ),
        )
        self.open_patch = patch.object(article_api, 'open_storage', lambda: _AsyncContext(SimpleNamespace()))
        # No meta table in the fake storage: hand the route its media settings.
        self.settings_patch = patch.object(article_api, 'get_media_settings', AsyncMock(return_value={}))
        self.container_patch = patch.object(
            article_api,
            'build_downloader_container',
            lambda **kwargs: _AsyncContext(SimpleNamespace(downloader=self.downloader)),
        )
        self.thread_patch = patch.object(article_api.threading, 'Thread', _DeferredThread)
        for active in (
            self.visibility_patch,
            self.row_patch,
            self.open_patch,
            self.settings_patch,
            self.container_patch,
            self.thread_patch,
        ):
            active.start()
            self.addCleanup(active.stop)
        return TestClient(app)

    def _auth(self, user_id: int | None) -> dict[str, str]:
        return {} if user_id is None else {'x-test-user': str(user_id)}

    def test_an_anonymous_caller_cannot_trigger_or_read(self) -> None:
        client = self._client(visible={(1, ARTICLE_ID)})

        self.assertEqual(401, client.post(f'/api/article/{ARTICLE_ID}/refetch').status_code)
        self.assertEqual(401, client.get('/api/article/refetch/whatever').status_code)
        self.assertEqual([], _DeferredThread.targets)

    def test_a_caller_cannot_refetch_an_invisible_article(self) -> None:
        client = self._client(visible={(1, ARTICLE_ID)})

        response = client.post(f'/api/article/{ARTICLE_ID}/refetch', headers=self._auth(2))

        self.assertEqual(404, response.status_code)
        self.downloader.download_from_url.assert_not_awaited()
        self.assertEqual([], _DeferredThread.targets)

    def test_an_allowed_article_starts_the_refetch(self) -> None:
        client = self._client(visible={(1, ARTICLE_ID)})

        response = client.post(f'/api/article/{ARTICLE_ID}/refetch', headers=self._auth(1))

        self.assertEqual(200, response.status_code)
        self.assertEqual('started', response.json()['status'])
        self.assertEqual(1, len(_DeferredThread.targets))
        _DeferredThread.targets[0]()
        self.downloader.download_from_url.assert_awaited_once_with(
            'https://mp.weixin.qq.com/s/AbCdEf',
            with_images=True,
            biz='Mz123',
            article_id='slug-1',
        )

    def test_the_creator_reads_the_task_without_owner_bookkeeping(self) -> None:
        client = self._client(visible={(1, ARTICLE_ID)})
        created = client.post(f'/api/article/{ARTICLE_ID}/refetch', headers=self._auth(1)).json()
        _DeferredThread.targets[0]()

        response = client.get(f'/api/article/refetch/{created["task_id"]}', headers=self._auth(1))

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {'task_id', 'status', 'phase', 'started_at', 'finished_at', 'error'},
            set(response.json()),
        )
        self.assertEqual('done', response.json()['status'])
        self.assertNotIn('user_id', response.json())

    def test_a_foreign_or_unknown_task_is_a_generic_404(self) -> None:
        client = self._client(visible={(1, ARTICLE_ID)})
        created = client.post(f'/api/article/{ARTICLE_ID}/refetch', headers=self._auth(1)).json()

        foreign = client.get(f'/api/article/refetch/{created["task_id"]}', headers=self._auth(2))
        unknown = client.get('/api/article/refetch/does-not-exist', headers=self._auth(1))

        self.assertEqual(404, foreign.status_code)
        self.assertEqual(404, unknown.status_code)
        self.assertEqual('Task not found', foreign.json()['error'])

    def test_a_client_supplied_user_id_cannot_bypass_the_creator_check(self) -> None:
        client = self._client(visible={(1, ARTICLE_ID)})
        # The body/query say "2", but only the authenticated header decides.
        created = client.post(
            f'/api/article/{ARTICLE_ID}/refetch?user_id=2',
            headers=self._auth(1),
            json={'user_id': 2},
        ).json()

        self.assertEqual(
            404, client.get(f'/api/article/refetch/{created["task_id"]}', headers=self._auth(2)).status_code
        )
        self.assertEqual(
            200, client.get(f'/api/article/refetch/{created["task_id"]}', headers=self._auth(1)).status_code
        )


class _LockAssertingTask(Mapping):
    """A task mapping that refuses to be read while the refetch lock is free.

    The worker mutates its task entry under ``_refetch_lock``, so any field read
    from outside the lock races with it. This double turns such a read into an
    immediate failure instead of an intermittent one.
    """

    def __init__(self, fields: dict) -> None:
        self._fields = fields

    def _assert_locked(self) -> None:
        if not article_api._refetch_lock.locked():
            raise AssertionError('refetch task read outside _refetch_lock')

    def __getitem__(self, key):
        self._assert_locked()
        return self._fields[key]

    def __iter__(self):
        self._assert_locked()
        return iter(self._fields)

    def __len__(self) -> int:
        self._assert_locked()
        return len(self._fields)


class RefetchStatusSnapshotTest(unittest.TestCase):
    """The status handler must copy the task under the lock, not read it after."""

    def setUp(self) -> None:
        with article_api._refetch_lock:
            article_api._refetch_tasks.clear()

    def tearDown(self) -> None:
        with article_api._refetch_lock:
            article_api._refetch_tasks.clear()

    def test_the_creator_response_does_not_read_the_live_task(self) -> None:
        fields = {
            'task_id': 'task-1',
            'user_id': 1,
            'status': 'running',
            'phase': 'downloading',
            'started_at': 1.0,
            'error': None,
        }
        with article_api._refetch_lock:
            article_api._refetch_tasks['task-1'] = _LockAssertingTask(fields)

        status = article_api.get_refetch_status('task-1', user=_user(1))

        self.assertEqual({key: value for key, value in fields.items() if key != 'user_id'}, status)


if __name__ == '__main__':
    unittest.main()
