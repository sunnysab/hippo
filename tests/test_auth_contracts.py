"""The auth and API-key contracts: typed requests, and what a response may carry.

The request bodies are the declared shape of a route, so a field the route needs
is required here and a wrong type is answered as a validation failure. The
response models must not drop a row field the caller relies on, and must never
publish a secret: the API key is shown once, and the session token hash is never
returned.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from _openapi import document as _document
from _openapi import resolve as _resolve
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hippo.api import deps
from hippo.api.errors import install_exception_handlers
from hippo.api.routers import apikey as apikey_api
from hippo.api.routers import auth as auth_api
from hippo.api.schemas.auth import ApiKeyCreateRequest, EmailChangeRequest, LoginRequest


class _NullTransaction:
    async def __aenter__(self) -> _NullTransaction:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _apikey_client(storage: SimpleNamespace) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(apikey_api.router, prefix='/api')
    app.dependency_overrides[deps.get_storage] = lambda: storage
    app.dependency_overrides[deps.current_user] = lambda: SimpleNamespace(id=1, username='alice', role='user')
    return TestClient(app)


def _auth_client(storage: SimpleNamespace) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_api.router, prefix='/api')
    app.dependency_overrides[deps.get_storage] = lambda: storage
    app.dependency_overrides[deps.current_user] = lambda: SimpleNamespace(id=1, username='alice', role='user')
    return TestClient(app)


def _key_row(**extra: Any) -> dict[str, Any]:
    return {
        'id': 5,
        'name': 'CI',
        'prefix': 'abcd1234',
        'scopes': ['read'],
        'last_used_at': None,
        'last_used_ip': None,
        'expires_at': None,
        'created_at': None,
        **extra,
    }


class RequestContractTest(unittest.TestCase):
    def test_the_login_body_needs_both_credentials(self) -> None:
        with self.assertRaises(ValidationError):
            LoginRequest.model_validate({'username': 'alice'})
        self.assertEqual('alice', LoginRequest.model_validate({'username': 'alice', 'password': 'secret'}).username)

    def test_an_email_change_can_clear_the_address(self) -> None:
        body = EmailChangeRequest.model_validate({'current_password': 'secret', 'email': ''})

        self.assertEqual('', body.email)

    def test_creating_a_key_needs_no_fields(self) -> None:
        body = ApiKeyCreateRequest()

        self.assertIsNone(body.name)
        self.assertIsNone(body.scopes)
        self.assertIsNone(body.expires_in_days)

    def test_a_non_integer_expiry_is_a_validation_failure(self) -> None:
        with self.assertRaises(ValidationError):
            ApiKeyCreateRequest.model_validate({'expires_in_days': 'soon'})


class KeyResponseContractTest(unittest.TestCase):
    def test_the_key_list_publishes_only_its_public_columns(self) -> None:
        # The row below holds two hashes and a column from the future. None of
        # them may reach the client: the model is an allowlist, not a passthrough.
        row = _key_row(secret_hash='sha256-of-the-key', key_hash='sha256-of-key-', future_column='kept')
        storage = SimpleNamespace(
            transaction=_NullTransaction,
            api_keys=SimpleNamespace(list_for_user=AsyncMock(return_value=[row])),
        )

        payload = _apikey_client(storage).get('/api/apikey').json()

        self.assertEqual(
            {'id', 'name', 'prefix', 'scopes', 'last_used_at', 'last_used_ip', 'expires_at', 'created_at'},
            set(payload['keys'][0]),
        )
        self.assertNotIn('secret_hash', str(payload))
        self.assertNotIn('key_hash', str(payload))
        self.assertEqual(['read', 'write'], payload['scopes'])

    def test_issuing_a_key_returns_the_secret_exactly_once(self) -> None:
        storage = SimpleNamespace(
            transaction=_NullTransaction,
            api_keys=SimpleNamespace(
                create=AsyncMock(return_value=(_key_row(secret_hash='sha256-of-the-key'), 'hippo_secret'))
            ),
            audit=SimpleNamespace(record=AsyncMock()),
        )

        created = _apikey_client(storage).post('/api/apikey', json={'name': 'CI'}).json()

        self.assertEqual('hippo_secret', created['secret'])
        # The secret is this response's one addition; the row's own hashes are not.
        self.assertNotIn('secret_hash', created)
        listed = (
            _apikey_client(
                SimpleNamespace(
                    transaction=_NullTransaction,
                    api_keys=SimpleNamespace(list_for_user=AsyncMock(return_value=[_key_row(secret_hash='sha256')])),
                )
            )
            .get('/api/apikey')
            .json()
        )
        self.assertNotIn('secret', str(listed))

    def test_the_document_promises_no_secret_either(self) -> None:
        spec = _document()
        listed = _resolve(
            spec, spec['paths']['/api/apikey']['get']['responses']['200']['content']['application/json']['schema']
        )
        key = _resolve(spec, _resolve(spec, listed)['properties']['keys']['items'])
        self.assertNotIn('secret', key['properties'])
        created = _resolve(
            spec, spec['paths']['/api/apikey']['post']['responses']['201']['content']['application/json']['schema']
        )
        self.assertIn('secret', created['properties'])

    def test_the_session_list_documents_no_token_hash(self) -> None:
        spec = _document()
        schema = _resolve(
            spec, spec['paths']['/api/auth/session']['get']['responses']['200']['content']['application/json']['schema']
        )
        session = _resolve(spec, _resolve(spec, schema)['properties']['sessions']['items'])
        self.assertNotIn('token_hash', session['properties'])
        self.assertIn('user_agent', session['properties'])

    def test_a_session_row_cannot_carry_its_token_hash_out(self) -> None:
        # The model is closed, so the hash is dropped even if the row below it
        # still holds one: the route's own pop is not the only thing protecting it.
        storage = SimpleNamespace(
            sessions=SimpleNamespace(
                list_for_user=AsyncMock(
                    return_value=[
                        {
                            'token_hash': 'sha256-of-the-token',
                            'user_agent': 'test',
                            'ip': '127.0.0.1',
                            'created_at': None,
                            'expires_at': None,
                        }
                    ]
                )
            )
        )

        payload = _auth_client(storage).get('/api/auth/session').json()

        self.assertEqual(1, len(payload['sessions']))
        self.assertEqual('test', payload['sessions'][0]['user_agent'])
        self.assertNotIn('token_hash', str(payload))


if __name__ == '__main__':
    unittest.main()
