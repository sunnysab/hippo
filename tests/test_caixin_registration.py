"""Caixin credentials and subscriptions use the existing source machinery."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.api.routers import admin as admin_api
from hippo.api.routers import source as source_api
from hippo.api.schemas.admin import MediaSettingsOut, MediaSettingsUpdateRequest
from hippo.api.schemas.source import CredentialListOut
from hippo.container import build_sync_container
from hippo.media_settings import default_media_settings, normalize_media_settings, resolve_proxy
from hippo.sources.credentials import CredentialError, clean
from hippo.sources.detect import detect_source
from hippo.sources.probe import probe_credential
from hippo.sources.registry import MediaSources, source_names

COOKIE = 'UID=123; SA_USER_auth=sentinel-auth'


class CaixinCredentialTest(unittest.TestCase):
    def test_uid_and_auth_are_required_and_duplicates_are_refused(self) -> None:
        self.assertEqual(COOKIE, clean('caixin', COOKIE))
        for secret in (
            'UID=123',
            'UID=0; SA_USER_auth=x',
            'UID=-1; SA_USER_auth=x',
            'UID=' + '9' * 5000 + '; SA_USER_auth=x',
            'UID=123; SA_USER_auth=',
            'UID=123; UID=456; SA_USER_auth=x',
            'UID=123; SA_USER_auth=x\x00',
        ):
            with self.subTest(secret=secret[:30]), self.assertRaises(CredentialError):
                clean('caixin', secret)

    def test_admin_responses_and_audit_values_never_contain_the_secret(self) -> None:
        settings = normalize_media_settings({'caixin_cookie': COOKIE})
        public = admin_api._public_media_settings(settings)
        self.assertTrue(public['caixin_cookie_set'])
        self.assertNotIn('sentinel-auth', str(public))
        self.assertTrue(MediaSettingsOut.model_validate(public).caixin_cookie_set)
        self.assertEqual(COOKIE, MediaSettingsUpdateRequest(caixin_cookie=COOKIE).caixin_cookie)
        self.assertFalse(
            CredentialListOut.model_validate(
                {
                    'bilibili': {'configured': False},
                    'youtube': {'configured': False},
                }
            ).caixin.configured
        )

    def test_caixin_uses_its_own_proxy_or_the_instance_default(self) -> None:
        settings = normalize_media_settings({'proxy_default': 'http://proxy.example:8082'})
        self.assertEqual('http://proxy.example:8082', resolve_proxy(settings, 'caixin'))
        settings = normalize_media_settings({'proxy_caixin': 'direct'}, current=settings)
        self.assertEqual('', resolve_proxy(settings, 'caixin'))
        self.assertIn('caixin', source_names())
        self.assertEqual('caixin', detect_source('https://international.caixin.com/'))
        self.assertEqual('podcast', detect_source('https://caixin.com.attacker.example/'))


class CaixinRegistrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_caixin_credentials_never_fall_back_to_youtube(self) -> None:
        storage = SimpleNamespace(credentials=SimpleNamespace(for_biz=AsyncMock(return_value=[])))
        sources = MediaSources(
            storage=storage,
            settings={**default_media_settings(), 'caixin_cookie': COOKIE, 'youtube_cookies': 'youtube-secret'},
        )
        try:
            self.assertEqual((COOKIE,), await sources._credentials_for('caixin')('caixin:132'))
            storage.credentials.for_biz.assert_awaited_once_with('caixin:132', 'caixin')
        finally:
            await sources.aclose()

    async def test_personal_credential_can_preview_with_transcription_disabled(self) -> None:
        storage = SimpleNamespace(credentials=SimpleNamespace(get=AsyncMock(return_value=COOKIE)))
        with patch(
            'hippo.api.routers.source.get_media_settings',
            new=AsyncMock(return_value={'enabled': False}),
        ):
            sources = await source_api._media_sources(storage, SimpleNamespace(id=1), source='caixin')
        try:
            info = await sources.preview('caixin', 'https://international.caixin.com/')
            self.assertEqual('caixin:132', info.biz)
            self.assertEqual((COOKIE,), await sources._credentials_for('caixin')(''))
        finally:
            await sources.aclose()

    async def test_disabling_transcription_keeps_caixin_and_text_feeds(self) -> None:
        container = build_sync_container(
            storage=SimpleNamespace(),
            enable_download=False,
            enable_images=False,
            media_settings={'enabled': False},
            sources={'caixin'},
        )
        try:
            self.assertEqual({'caixin', 'feed'}, set(container.adapters))
        finally:
            await container.media_sources.aclose()

    async def test_probe_reports_paid_content_access_and_hides_unexpected_errors(self) -> None:
        client = SimpleNamespace(probe=AsyncMock(return_value='Paid article'), aclose=AsyncMock())
        with patch('hippo.sources.probe.CaixinSource', return_value=client):
            result = await probe_credential(source='caixin', cookie=COOKIE)
            self.assertTrue(result.ok)
            self.assertEqual('Paid article', result.account)
            client.probe.side_effect = RuntimeError(COOKIE)
            result = await probe_credential(source='caixin', cookie=COOKIE)
            self.assertFalse(result.ok)
            self.assertNotIn('sentinel-auth', result.detail)
        self.assertEqual(2, client.aclose.await_count)
