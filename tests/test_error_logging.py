"""What a failed request records.

The caller's identity and a stable error code used to be missing: the log said
only which path failed and with what status, and clients could not tell one 401
from another without matching prose.
"""

import logging
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hippo.api.deps import NOT_SIGNED_IN, SESSION_MISSING
from hippo.exceptions import ApiError
from hippo.observability import logging as obs_logging


class ApiErrorLogTest(unittest.TestCase):
    """A failed request must say who called and why, in queryable fields."""

    def setUp(self) -> None:
        self.lines: list[dict[str, object]] = []
        lines = self.lines

        class _Capture(logging.Handler):
            """Collect the event dict structlog hands the sinks."""

            def emit(self, record: logging.LogRecord) -> None:
                if isinstance(record.msg, dict):
                    lines.append(dict(record.msg))

        obs_logging.configure_logging(level=logging.DEBUG, json_output=True, extra_handlers=[_Capture()])
        self.addCleanup(obs_logging.shutdown_logging)

    def _app(self) -> FastAPI:
        from hippo.api.errors import install_exception_handlers
        from hippo.api.middleware import install_request_context

        app = FastAPI()
        install_request_context(app)
        install_exception_handlers(app)
        return app

    def _failed(self) -> dict[str, object]:
        # The pipeline renders on a listener thread; drain it before inspecting.
        obs_logging.shutdown_logging()
        self.assertTrue(self.lines, 'the error handler logged nothing')
        return next(line for line in self.lines if line.get('event') == 'request_failed')

    def test_the_failure_carries_the_caller_and_a_stable_code(self) -> None:
        app = self._app()

        @app.get('/api/secret')
        async def secret():
            raise ApiError(NOT_SIGNED_IN, status=401, code=SESSION_MISSING)

        response = TestClient(app).get(
            '/api/secret',
            headers={'user-agent': 'probe/1', 'host': 'hippo.lab'},
        )

        self.assertEqual(401, response.status_code)
        line = self._failed()
        self.assertEqual(SESSION_MISSING, line['code'])
        self.assertEqual(401, line['status'])
        self.assertEqual('/api/secret', line['path'])
        self.assertEqual('GET', line['method'])
        self.assertEqual('testclient', line['client_ip'])
        self.assertEqual('hippo.lab', line['http_host'])
        self.assertEqual('probe/1', line['user_agent'])
        # A 401 is a routine expiry the client handles, so it stays quiet.
        self.assertEqual('info', line['level'])

    def test_the_body_carries_the_code_for_the_client_to_branch_on(self) -> None:
        app = self._app()

        @app.get('/api/secret')
        async def secret():
            raise ApiError(NOT_SIGNED_IN, status=401, code=SESSION_MISSING)

        self.assertEqual(
            {'error': NOT_SIGNED_IN, 'code': SESSION_MISSING},
            TestClient(app).get('/api/secret').json(),
        )

    def test_a_server_error_is_logged_at_error_level(self) -> None:
        app = self._app()

        @app.get('/api/boom')
        async def boom():
            raise ApiError('上游不可用', status=502, code='daemon.unreachable')

        response = TestClient(app, raise_server_exceptions=False).get('/api/boom')

        self.assertEqual(502, response.status_code)
        self.assertEqual('error', self._failed()['level'])

    def test_other_client_errors_stay_visible_at_warning(self) -> None:
        """Only the 401 is demoted: a 403 or a 429 must survive level=WARNING."""
        app = self._app()

        @app.get('/api/forbidden')
        async def forbidden():
            raise ApiError('需要管理员权限', status=403, code='http_403')

        response = TestClient(app).get('/api/forbidden')

        self.assertEqual(403, response.status_code)
        self.assertEqual('warning', self._failed()['level'])

    def test_the_cause_is_logged_but_never_returned(self) -> None:
        app = self._app()

        @app.get('/api/boom')
        async def boom():
            try:
                raise ValueError('psql://user:secret@host/db refused')
            except ValueError as exc:
                raise ApiError('上游不可用', status=502, code='daemon.unreachable') from exc

        response = TestClient(app, raise_server_exceptions=False).get('/api/boom')

        self.assertNotIn('secret', response.text)
        self.assertIn('secret', str(self._failed().get('exception', '')))

    def test_an_unexpected_failure_still_names_the_caller(self) -> None:
        """A 500 is handled outside the context middleware, so it reads the request itself."""
        app = self._app()

        @app.get('/api/boom')
        async def boom():
            raise RuntimeError('unexpected')

        response = TestClient(app, raise_server_exceptions=False).get(
            '/api/boom', headers={'user-agent': 'probe/1', 'host': 'hippo.lab'}
        )

        self.assertEqual(500, response.status_code)
        obs_logging.shutdown_logging()
        line = next(item for item in self.lines if item.get('event') == 'request_crashed')
        self.assertEqual('testclient', line['client_ip'])
        self.assertEqual('hippo.lab', line['http_host'])
        self.assertEqual('probe/1', line['user_agent'])

    def test_an_unexpected_failure_does_not_leak_internals(self) -> None:
        app = self._app()

        @app.get('/api/boom')
        async def boom():
            raise RuntimeError('psql://user:secret@host/db is down')

        response = TestClient(app, raise_server_exceptions=False).get('/api/boom')

        self.assertEqual(
            {'error': 'Internal server error', 'code': 'internal_error'},
            response.json(),
        )
        self.assertNotIn('secret', response.text)
