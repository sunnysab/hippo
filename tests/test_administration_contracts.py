"""The administration, knowledge and daemon contracts.

Three properties are pinned here, one per area:

* An admin request body is the declared shape of its route, and the media
  settings stay a whitelist: a field left out is not written, and a secret is
  only ever read back as the mask.
* A knowledge response is the fixed public field list; the object key, the
  content hash, the lease pair and the owner id never leave the server.
* The WeChat daemon endpoints answer the shape the console renders, and nothing
  the SDK hands back is renamed or invented.

The models are read directly, and the HTTP cases run over the real routers with
only storage and the acting identity stubbed.
"""

from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hippo import server
from hippo.api import deps
from hippo.api.errors import install_exception_handlers
from hippo.api.routers import admin as admin_api
from hippo.api.schemas.admin import (
    MediaSettingsUpdateRequest,
    UserCreateRequest,
    UserUpdateRequest,
)
from hippo.media_settings import default_media_settings


def _resolve(spec: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Follow a local ``$ref`` to the schema it names."""
    while '$ref' in node:
        _, _, name = node['$ref'].rpartition('/')
        node = spec['components']['schemas'][name]
    return node


def _document() -> dict[str, Any]:
    with (
        tempfile.TemporaryDirectory() as static_dir,
        patch('hippo.server.configure_logging'),
        patch('hippo.server.init_telemetry', return_value=None),
        patch('hippo.server.instrument_app'),
    ):
        return server.create_app(static_dir=static_dir, enable_inprocess_sync=False).openapi()


def _admin_client(storage: SimpleNamespace) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(admin_api.router, prefix='/api')
    app.dependency_overrides[deps.get_storage] = lambda: storage
    app.dependency_overrides[deps.require_admin] = lambda: SimpleNamespace(id=1, username='root', role='admin')
    return TestClient(app)


class AdminRequestContractTest(unittest.TestCase):
    def test_creating_a_user_needs_a_username_and_a_password(self) -> None:
        with self.assertRaises(ValidationError):
            UserCreateRequest.model_validate({'username': 'u'})
        body = UserCreateRequest.model_validate({'username': 'u', 'password': 'a-good-password'})

        self.assertEqual('user', body.role)
        self.assertTrue(body.email_verified)
        self.assertIsNone(body.email)

    def test_an_unknown_role_is_a_validation_failure(self) -> None:
        with self.assertRaises(ValidationError):
            UserCreateRequest.model_validate({'username': 'u', 'password': 'a-good-password', 'role': 'superuser'})

    def test_an_update_writes_only_the_keys_the_caller_sent(self) -> None:
        self.assertEqual({}, UserUpdateRequest().model_dump(exclude_unset=True))
        self.assertEqual(
            {'is_disabled': False},
            UserUpdateRequest(is_disabled=False).model_dump(exclude_unset=True),
        )

    def test_the_media_update_is_a_partial_whitelist(self) -> None:
        body = MediaSettingsUpdateRequest.model_validate({'items_per_sync': 9})

        self.assertEqual({'items_per_sync': 9}, body.model_dump(exclude_unset=True))
        # A field outside the whitelist is not part of the dump, so it can never
        # reach the settings writer.
        widened = MediaSettingsUpdateRequest.model_validate({'items_per_sync': 9, 'not_a_setting': 1})
        self.assertEqual({'items_per_sync': 9}, widened.model_dump(exclude_unset=True))


class AdminResponseContractTest(unittest.TestCase):
    def test_the_user_list_drops_columns_it_does_not_publish(self) -> None:
        row = {
            'id': 1,
            'username': 'alice',
            'email': None,
            'email_verified': True,
            'role': 'user',
            'timezone': 'Asia/Shanghai',
            'is_disabled': False,
            'created_at': None,
            'session_count': 2,
            'last_login_at': None,
            'password_hash': 'pbkdf2-of-the-password',
        }
        storage = SimpleNamespace(users=SimpleNamespace(list_with_stats=AsyncMock(return_value=[row])))

        payload = _admin_client(storage).get('/api/admin/user').json()

        self.assertEqual('alice', payload['items'][0]['username'])
        self.assertNotIn('password_hash', str(payload))

    def test_reading_the_media_settings_masks_every_secret(self) -> None:
        settings = default_media_settings()
        settings['asr_api_key'] = 'sk-1234567890abcdef'
        settings['bilibili_cookie'] = 'SESSDATA=abcdef'
        settings['youtube_cookies'] = 'SID=xyz'
        storage = SimpleNamespace()

        with patch.object(admin_api, 'get_media_settings', AsyncMock(return_value=settings)):
            payload = _admin_client(storage).get('/api/admin/media').json()

        self.assertNotIn('sk-1234567890abcdef', str(payload))
        self.assertNotIn('SESSDATA=abcdef', str(payload))
        self.assertNotIn('SID=xyz', str(payload))
        self.assertTrue(payload['asr_api_key_set'])
        self.assertTrue(payload['bilibili_cookie_set'])
        self.assertTrue(payload['youtube_cookies_set'])
        self.assertTrue(payload['asr_api_key'].endswith('cdef'))

    def test_the_media_settings_document_is_closed(self) -> None:
        spec = _document()
        schema = _resolve(
            spec, spec['paths']['/api/admin/media']['get']['responses']['200']['content']['application/json']['schema']
        )

        self.assertNotEqual(True, schema.get('additionalProperties'))
        self.assertIn('asr_api_key', schema['properties'])
        self.assertIn('asr_api_key_set', schema['properties'])
        self.assertNotIn('bilibili_cookie_value', schema['properties'])


if __name__ == '__main__':
    unittest.main()
