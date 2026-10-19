"""The settings, preferences and test-email contracts.

The settings body is the one place a wrong idea about SMTP lives: the password
is write-only, so what these pin is that the read side only ever returns the
mask, that a round-tripped mask never overwrites the stored secret, and that an
unknown key is refused (422 ``request.invalid``) instead of silently ignored.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hippo.api import deps
from hippo.api.errors import VALIDATION_FAILED, install_exception_handlers
from hippo.api.routers import settings as settings_api
from hippo.api.schemas.settings import (
    EmailSettingsUpdate,
    EmailTestRequest,
    PreferencesUpdateRequest,
    SyncSettingsUpdateRequest,
)

SYNC_DEFAULTS: dict[str, object] = {
    'enabled': False,
    'interval_minutes': 60,
    'sleep_seconds': 15.0,
    'skip_minutes': 30,
    'download_content': True,
    'download_images': True,
    'article_exclude_keywords': '',
    'alert_enabled': False,
    'alert_email': '',
}

EMAIL = {
    'smtp_host': 'smtp.example.com',
    'smtp_port': 587,
    'smtp_user': 'alerts',
    'smtp_password': 'supersecret',
    'smtp_tls': True,
    'from_email': 'no-reply@example.com',
}


def _client() -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(settings_api.router, prefix='/api')
    admin = SimpleNamespace(id=1, username='root', role='admin')
    app.dependency_overrides[deps.get_storage] = lambda: SimpleNamespace()
    app.dependency_overrides[deps.current_user] = lambda: admin
    app.dependency_overrides[deps.require_admin] = lambda: admin
    return TestClient(app)


class SettingsRequestContractTest(unittest.TestCase):
    def test_an_unknown_key_is_refused(self) -> None:
        for model, payload in (
            (SyncSettingsUpdateRequest, {'interval_minuts': 5}),
            (PreferencesUpdateRequest, {'timezon': 'UTC'}),
            (EmailTestRequest, {'toEmails': ['a@b.c']}),
            (EmailSettingsUpdate, {'smtp_pass': 'x'}),
        ):
            with self.subTest(model=model.__name__), self.assertRaises(ValidationError):
                model.model_validate(payload)

    def test_the_instance_article_filter_is_a_known_field(self) -> None:
        body = SyncSettingsUpdateRequest.model_validate({'article_exclude_keywords': 'promo\nad'})

        self.assertEqual({'article_exclude_keywords'}, body.model_fields_set)

    def test_a_bad_smtp_port_is_a_validation_failure(self) -> None:
        with self.assertRaises(ValidationError):
            EmailSettingsUpdate.model_validate({'smtp_port': 0})

    def test_only_the_keys_sent_are_written(self) -> None:
        self.assertEqual(set(), SyncSettingsUpdateRequest().model_fields_set)
        body = SyncSettingsUpdateRequest.model_validate({'enabled': True, 'email': {'smtp_host': 'h'}})
        self.assertEqual({'enabled', 'email'}, body.model_fields_set)
        assert body.email is not None
        self.assertEqual({'smtp_host'}, body.email.model_fields_set)


class SettingsHttpContractTest(unittest.TestCase):
    def test_the_read_never_returns_the_stored_password(self) -> None:
        client = _client()
        with (
            patch.object(settings_api, 'load_sync_settings', AsyncMock(return_value=dict(SYNC_DEFAULTS))),
            patch.object(settings_api, 'get_email_settings', AsyncMock(return_value=dict(EMAIL))),
        ):
            response = client.get('/api/settings')

        self.assertEqual(200, response.status_code)
        payload = response.json()['email']
        self.assertNotIn('supersecret', response.text)
        self.assertEqual('••••cret', payload['smtp_password'])
        self.assertTrue(payload['smtp_password_set'])

    def test_an_unknown_patch_key_is_a_422_request_invalid(self) -> None:
        client = _client()
        response = client.patch('/api/settings', json={'interval_minuts': 5})

        self.assertEqual(422, response.status_code)
        self.assertEqual(VALIDATION_FAILED, response.json()['code'])

    def test_the_round_tripped_mask_does_not_overwrite_the_password(self) -> None:
        client = _client()
        set_email = AsyncMock(return_value=dict(EMAIL))
        with (
            patch.object(settings_api, 'load_sync_settings', AsyncMock(return_value=dict(SYNC_DEFAULTS))),
            patch.object(settings_api, 'save_sync_settings', AsyncMock(return_value=dict(SYNC_DEFAULTS))),
            patch.object(settings_api, 'set_email_settings', set_email),
            patch.object(settings_api, 'get_email_settings', AsyncMock(return_value=dict(EMAIL))),
        ):
            response = client.patch('/api/settings', json={'email': {'smtp_password': '••••cret'}})

        self.assertEqual(200, response.status_code)
        set_email.assert_not_awaited()

    def test_an_empty_password_clears_it(self) -> None:
        client = _client()
        set_email = AsyncMock(return_value=dict(EMAIL))
        with (
            patch.object(settings_api, 'load_sync_settings', AsyncMock(return_value=dict(SYNC_DEFAULTS))),
            patch.object(settings_api, 'save_sync_settings', AsyncMock(return_value=dict(SYNC_DEFAULTS))),
            patch.object(settings_api, 'set_email_settings', set_email),
            patch.object(settings_api, 'get_email_settings', AsyncMock(return_value=dict(EMAIL))),
        ):
            response = client.patch('/api/settings', json={'email': {'smtp_password': ''}})

        self.assertEqual(200, response.status_code)
        self.assertEqual({'smtp_password': ''}, set_email.await_args.args[1])

    def test_an_explicit_null_password_is_ignored(self) -> None:
        client = _client()
        set_email = AsyncMock(return_value=dict(EMAIL))
        with (
            patch.object(settings_api, 'load_sync_settings', AsyncMock(return_value=dict(SYNC_DEFAULTS))),
            patch.object(settings_api, 'save_sync_settings', AsyncMock(return_value=dict(SYNC_DEFAULTS))),
            patch.object(settings_api, 'set_email_settings', set_email),
            patch.object(settings_api, 'get_email_settings', AsyncMock(return_value=dict(EMAIL))),
        ):
            response = client.patch('/api/settings', json={'email': {'smtp_password': None}})

        self.assertEqual(200, response.status_code)
        set_email.assert_not_awaited()

    def test_a_partial_patch_writes_only_the_fields_sent(self) -> None:
        client = _client()
        save = AsyncMock(return_value=dict(SYNC_DEFAULTS))
        set_email = AsyncMock(return_value=dict(EMAIL))
        with (
            patch.object(settings_api, 'load_sync_settings', AsyncMock(return_value=dict(SYNC_DEFAULTS))),
            patch.object(settings_api, 'save_sync_settings', save),
            patch.object(settings_api, 'set_email_settings', set_email),
            patch.object(settings_api, 'get_email_settings', AsyncMock(return_value=dict(EMAIL))),
        ):
            response = client.patch(
                '/api/settings',
                json={'enabled': True, 'interval_minutes': 5, 'email': {'smtp_host': 'smtp.new'}},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual({'enabled': True, 'interval_minutes': 5}, save.await_args.args[1])
        self.assertEqual({'smtp_host': 'smtp.new'}, set_email.await_args.args[1])

    def test_an_invalid_test_email_port_is_a_422(self) -> None:
        client = _client()
        response = client.post(
            '/api/settings/test-email',
            json={'to_email': 'ops@example.com', 'email': {'smtp_port': 0}},
        )

        self.assertEqual(422, response.status_code)
        self.assertEqual(VALIDATION_FAILED, response.json()['code'])

    def test_preferences_reject_an_unknown_key(self) -> None:
        client = _client()
        response = client.patch('/api/settings/preferences', json={'timezon': 'UTC'})

        self.assertEqual(422, response.status_code)
        self.assertEqual(VALIDATION_FAILED, response.json()['code'])


if __name__ == '__main__':
    unittest.main()
