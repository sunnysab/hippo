"""The generated API document.

/docs is built from the same dependency graph the routes enforce, so these tests
read the generated document rather than restating it: what they pin is that the
document does not promise a credential the code refuses, and that every failure
is described by the one body the API actually returns.
"""

from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

import hippo
from hippo import server
from hippo.api import deps
from hippo.api.errors import VALIDATION_FAILED, install_exception_handlers
from hippo.api.openapi import OPENAPI_TAGS

ROOT = Path(__file__).resolve().parent.parent
HTTP_METHODS = ('get', 'post', 'put', 'patch', 'delete')


class _Credentials(BaseModel):
    """A stand-in body: the envelope is a property of the handlers, not of one route."""

    username: str
    password: str


def _app() -> FastAPI:
    """The real app, with logging, telemetry and the static mount kept out of it."""
    with (
        tempfile.TemporaryDirectory() as static_dir,
        patch('hippo.server.configure_logging'),
        patch('hippo.server.init_telemetry', return_value=None),
        patch('hippo.server.instrument_app'),
    ):
        return server.create_app(static_dir=static_dir, enable_inprocess_sync=False)


def _operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, operation)
        for path, item in spec['paths'].items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    ]


class OpenApiDocumentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = _app().openapi()

    def test_the_document_is_branded(self) -> None:
        info = self.spec['info']
        self.assertEqual('Hippo API', info['title'])
        self.assertEqual('MIT', info['license']['name'])
        # The description is where the multi-source platform is explained.
        self.assertIn('WeChat public accounts', info['description'])
        self.assertIn('Bilibili', info['description'])
        self.assertIn('Knowledge bases', info['description'])

    def test_the_documented_version_is_the_project_version(self) -> None:
        # One number, owned by pyproject.toml, read back through the installed
        # distribution so the package and /docs cannot drift apart.
        if hippo.__version__ == '0.0.0+unknown':
            self.skipTest('hippo is not installed in this environment')
        self.assertEqual(hippo.__version__, self.spec['info']['version'])
        declared = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
        self.assertEqual(declared, hippo.__version__, 'pyproject.toml and the installed version disagree')

    def test_every_operation_has_exactly_one_known_tag(self) -> None:
        known = {tag['name'] for tag in OPENAPI_TAGS}
        for path, method, operation in _operations(self.spec):
            tags = operation.get('tags') or []
            self.assertEqual(1, len(tags), f'{method.upper()} {path} has {tags}')
            self.assertIn(tags[0], known, f'{method.upper()} {path}')

    def test_the_two_credentials_the_code_accepts_are_both_declared(self) -> None:
        schemes = self.spec['components']['securitySchemes']
        self.assertEqual('apiKey', schemes['hippo_session']['type'])
        self.assertEqual(deps.SESSION_COOKIE, schemes['hippo_session']['name'])
        self.assertEqual('cookie', schemes['hippo_session']['in'])
        self.assertEqual('bearer', schemes['api_key_bearer']['scheme'])
        self.assertEqual('X-API-Key', schemes['api_key_header']['name'])
        self.assertEqual('token', schemes['feed_token']['name'])

    def test_a_key_is_offered_wherever_the_code_accepts_one(self) -> None:
        expected = [{'hippo_session': []}, {'api_key_bearer': []}, {'api_key_header': []}]
        for path in ('/api/whoami', '/api/article', '/api/account', '/api/knowledge-base'):
            for method, operation in self.spec['paths'][path].items():
                if method in HTTP_METHODS:
                    self.assertEqual(expected, operation['security'], f'{method.upper()} {path}')

    def test_a_key_is_never_offered_where_the_code_refuses_one(self) -> None:
        # deps.API_KEY_FORBIDDEN_PREFIXES is the check that actually runs; the
        # document must not invite a key to a path that would answer 403.
        for path, method, operation in _operations(self.spec):
            if operation.get('security') is None:
                continue
            if not any(path.startswith(prefix) for prefix in deps.API_KEY_FORBIDDEN_PREFIXES):
                continue
            names = {name for requirement in operation['security'] for name in requirement}
            self.assertEqual({'hippo_session'}, names, f'{method.upper()} {path}')

    def test_only_the_routes_that_need_no_credential_ask_for_none(self) -> None:
        public = {
            (path, method) for path, method, operation in _operations(self.spec) if operation.get('security') is None
        }
        self.assertEqual(
            {
                ('/api/auth/login', 'post'),
                ('/api/auth/logout', 'post'),
                ('/api/auth/register', 'post'),
                ('/api/auth/verify', 'post'),
                ('/api/auth/reset-request', 'post'),
                ('/api/auth/reset', 'post'),
            },
            public,
        )

    def test_the_feed_reader_token_is_documented_only_where_it_works(self) -> None:
        alternatives = self.spec['paths']['/api/feed/mixed']['get']['security']
        self.assertIn({'feed_token': []}, alternatives)
        for path, method, operation in _operations(self.spec):
            if (path, method) == ('/api/feed/mixed', 'get'):
                continue
            for requirement in operation.get('security') or []:
                self.assertNotIn('feed_token', requirement, f'{method.upper()} {path}')

    def test_every_failure_status_points_at_the_one_error_body(self) -> None:
        error_body = self.spec['components']['schemas']['ErrorBody']
        self.assertEqual({'error', 'code'}, set(error_body['properties']))
        reference = {'$ref': '#/components/schemas/ErrorBody'}
        for path, method, operation in _operations(self.spec):
            responses = operation['responses']
            # FastAPI's own validation body is replaced: the handler answers in
            # the same envelope as every other failure.
            if '422' in responses:
                self.assertEqual(
                    reference,
                    responses['422']['content']['application/json']['schema'],
                    f'{method.upper()} {path}',
                )
            self.assertEqual(reference, responses['500']['content']['application/json']['schema'], path)
            if operation.get('security'):
                for status in ('401', '403'):
                    self.assertEqual(reference, responses[status]['content']['application/json']['schema'], path)

    def test_no_operation_still_describes_a_validation_body_that_is_never_sent(self) -> None:
        # The document must not offer a second error shape nobody can receive.
        self.assertNotIn('HTTPValidationError', self.spec['components']['schemas'])
        self.assertNotIn('ValidationError', self.spec['components']['schemas'])


class ErrorEnvelopeTest(unittest.TestCase):
    def test_a_malformed_body_answers_in_the_shared_envelope(self) -> None:
        app = FastAPI()
        install_exception_handlers(app)

        @app.post('/login')
        async def login(body: _Credentials) -> dict[str, bool]:
            return {'ok': True}

        client = TestClient(app, raise_server_exceptions=False)
        response = client.post('/login', json={'username': 'alice'})

        self.assertEqual(422, response.status_code)
        payload = response.json()
        self.assertEqual(VALIDATION_FAILED, payload['code'])
        # The field that failed is named, and the body has exactly the two keys
        # every other failure uses.
        self.assertIn('password', payload['error'])
        self.assertEqual({'error', 'code'}, set(payload))

    def test_a_request_that_matches_the_schema_still_works(self) -> None:
        app = FastAPI()
        install_exception_handlers(app)

        @app.post('/login')
        async def login(body: _Credentials) -> dict[str, str]:
            return {'username': body.username}

        response = TestClient(app).post('/login', json={'username': 'alice', 'password': 'secret'})

        self.assertEqual(200, response.status_code)
        self.assertEqual({'username': 'alice'}, response.json())

    def test_a_failure_raised_before_the_route_uses_the_same_envelope(self) -> None:
        # Starlette answers a method a route does not allow (and an unrouted
        # path) itself; both must still speak {error, code}.
        app = FastAPI()
        install_exception_handlers(app)

        @app.get('/thing')
        async def thing() -> dict[str, bool]:
            return {'ok': True}

        client = TestClient(app)
        method_not_allowed = client.post('/thing')
        unknown_path = client.get('/nothing-here')

        self.assertEqual(405, method_not_allowed.status_code)
        self.assertEqual({'error', 'code'}, set(method_not_allowed.json()))
        self.assertEqual('http_405', method_not_allowed.json()['code'])
        self.assertEqual(404, unknown_path.status_code)
        self.assertEqual('http_404', unknown_path.json()['code'])


if __name__ == '__main__':
    unittest.main()
