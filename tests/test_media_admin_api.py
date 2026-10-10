"""Media settings endpoints.

Two things must hold no matter what the console sends: a secret never leaves the
API whole, and the mask that comes back is never written on top of the real
value.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.api.routers import admin as admin_api
from hippo.media_settings import default_media_settings


def _settings(**overrides) -> dict:
    settings = default_media_settings()
    settings.update(overrides)
    return settings


class _NullTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _storage() -> SimpleNamespace:
    return SimpleNamespace(
        transaction=lambda: _NullTransaction(),
        audit=SimpleNamespace(record=AsyncMock()),
    )


class MediaSettingsSerializationTest(unittest.TestCase):
    def test_secrets_are_masked_and_flagged(self) -> None:
        public = admin_api._public_media_settings(
            _settings(asr_api_key='sk-1234567890abcdef', bilibili_cookie='SESSDATA=abcdef')
        )

        self.assertEqual('••••cdef', public['asr_api_key'])
        self.assertTrue(public['asr_api_key_set'])
        self.assertEqual('••••cdef', public['bilibili_cookie'])
        self.assertTrue(public['bilibili_cookie_set'])

    def test_an_empty_secret_reads_as_unset(self) -> None:
        public = admin_api._public_media_settings(_settings())

        self.assertEqual('', public['asr_api_key'])
        self.assertFalse(public['asr_api_key_set'])

    def test_a_round_tripped_mask_is_not_written_back(self) -> None:
        cleaned = admin_api._without_round_tripped_masks(
            {'asr_api_key': '••••cdef', 'bilibili_cookie': 'new-cookie', 'asr_tier': 'fast'}
        )

        self.assertNotIn('asr_api_key', cleaned)
        self.assertEqual('new-cookie', cleaned['bilibili_cookie'])
        self.assertEqual('fast', cleaned['asr_tier'])


class MediaSettingsEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_reading_hides_the_key(self) -> None:
        storage = _storage()

        with patch(
            'hippo.api.routers.admin.get_media_settings',
            new=AsyncMock(return_value=_settings(asr_api_key='sk-abcdefgh')),
        ):
            payload = await admin_api.read_media_settings(_=SimpleNamespace(id=1), storage=storage)

        self.assertEqual('••••efgh', payload['asr_api_key'])

    async def test_updating_audits_only_the_visible_change(self) -> None:
        storage = _storage()
        stored = _settings(asr_api_key='sk-abcdefgh')

        with (
            patch(
                'hippo.api.routers.admin.get_media_settings',
                new=AsyncMock(return_value=stored),
            ),
            patch('hippo.api.routers.admin.set_media_settings', new=AsyncMock(return_value=stored)) as setter,
        ):
            await admin_api.update_media_settings(
                SimpleNamespace(headers={}, client=SimpleNamespace(host='127.0.0.1')),
                body={'asr_api_key': '••••efgh', 'items_per_sync': 9},
                actor=SimpleNamespace(id=7),
                storage=storage,
            )

        # The mask must not reach the setter, or it would overwrite the real key.
        self.assertNotIn('asr_api_key', setter.call_args.args[1])
        self.assertEqual(9, setter.call_args.args[1]['items_per_sync'])
        storage.audit.record.assert_not_awaited()


class HealthUrlTest(unittest.TestCase):
    def test_v1_is_stripped_before_health(self) -> None:
        self.assertEqual(
            'http://192.168.130.202:8317/health',
            admin_api._health_url('http://192.168.130.202:8317/v1/'),
        )

    def test_an_endpoint_without_v1_is_kept(self) -> None:
        self.assertEqual(
            'http://asr.internal:9000/health',
            admin_api._health_url('http://asr.internal:9000'),
        )


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


class _FakeClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        self.url = url
        return self._response


class MediaProbeTest(unittest.IsolatedAsyncioTestCase):
    async def _probe(self, response) -> dict:
        with patch(
            'hippo.api.routers.admin.httpx.AsyncClient',
            return_value=_FakeClient(response),
        ):
            return await admin_api._probe_asr_tier('accurate', _settings())

    async def test_a_healthy_tier_reports_its_backend(self) -> None:
        result = await self._probe(_FakeResponse(200, {'status': 'ok', 'backend': 'qwen3-1.7b'}))

        self.assertTrue(result['ok'])
        self.assertEqual('backend=qwen3-1.7b', result['detail'])

    async def test_a_non_200_is_reported_not_raised(self) -> None:
        result = await self._probe(_FakeResponse(404, None))

        self.assertFalse(result['ok'])
        self.assertIn('HTTP 404', result['detail'])

    async def test_a_tier_without_an_endpoint_is_not_probed(self) -> None:
        result = await admin_api._probe_asr_tier('fast', _settings(asr_endpoints={'accurate': 'x', 'fast': ''}))

        self.assertFalse(result['ok'])
        self.assertEqual('未配置端点', result['detail'])


if __name__ == '__main__':
    unittest.main()
