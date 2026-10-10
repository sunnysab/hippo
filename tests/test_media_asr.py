"""ASR client behaviour.

Two things matter here: the failure vocabulary (a misconfigured backend must read
as one actionable Chinese sentence, not a stack trace) and the segment parsing
that the transcript timestamps are built on.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from hippo.media.asr import AsrClient, AsrError, client_for, health_url
from hippo.media_settings import default_media_settings


def _payload() -> dict:
    return {
        'text': '大家好啊',
        'language': 'zh',
        'duration': 63.2,
        'segments': [
            {'id': 0, 'start': 0.0, 'end': 29.4, 'text': '大家好啊'},
            {'id': 1, 'start': 29.4, 'end': 63.2, 'text': '今天呢'},
            {'id': 2, 'start': 63.2, 'end': 63.3, 'text': '   '},
        ],
    }


def _client(handler, **kwargs) -> AsrClient:
    transport = httpx.MockTransport(handler)
    return AsrClient(
        endpoint='http://asr.test:8317/v1',
        client=httpx.AsyncClient(transport=transport),
        **kwargs,
    )


class HealthUrlTest(unittest.TestCase):
    def test_v1_is_stripped_before_health(self) -> None:
        self.assertEqual('http://192.168.130.202:8317/health', health_url('http://192.168.130.202:8317/v1/'))

    def test_an_endpoint_without_v1_is_kept(self) -> None:
        self.assertEqual('http://asr.internal:9000/health', health_url('http://asr.internal:9000'))


class TranscribeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Kept alive for the whole test: a TemporaryDirectory that goes out of
        # scope deletes the file the client is about to upload.
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.audio = Path(self._dir.name) / 'part-000.mp3'
        self.audio.write_bytes(b'not really audio')

    async def test_text_and_segments_are_parsed(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=_payload())

        transcript = await _client(handler).transcribe(self.audio)

        self.assertEqual('大家好啊', transcript.text)
        self.assertEqual(2, len(transcript.segments))
        self.assertEqual(29.4, transcript.segments[0].end_seconds)
        self.assertEqual(63.2, transcript.duration_seconds)
        self.assertEqual('zh', transcript.language)

    async def test_the_upload_asks_for_verbose_json(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=_payload())

        await _client(handler, max_new_tokens=2048).transcribe(self.audio)

        request = seen[0]
        self.assertEqual('/v1/audio/transcriptions', request.url.path)
        self.assertTrue(request.headers['content-type'].startswith('multipart/form-data'))
        body = request.content
        self.assertIn(b'verbose_json', body)
        # A server whose own default is lower would truncate the chunk silently.
        self.assertIn(b'2048', body)

    async def test_the_key_is_sent_when_configured(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=_payload())

        await _client(handler, api_key='secret-key').transcribe(self.audio)

        self.assertEqual('Bearer secret-key', seen[0].headers['authorization'])

    async def test_an_unauthenticated_server_reads_as_an_auth_failure(self) -> None:
        transcript = _client(lambda request: httpx.Response(401, text='nope'))

        with self.assertRaises(AsrError) as ctx:
            await transcript.transcribe(self.audio)

        self.assertIn('认证失败', str(ctx.exception))

    async def test_a_server_error_carries_the_status(self) -> None:
        with self.assertRaises(AsrError) as ctx:
            await _client(lambda request: httpx.Response(500, text='boom')).transcribe(self.audio)

        self.assertIn('HTTP 500', str(ctx.exception))
        self.assertIn('boom', str(ctx.exception))

    async def test_a_body_that_is_not_json_is_reported(self) -> None:
        with self.assertRaises(AsrError) as ctx:
            await _client(lambda request: httpx.Response(200, text='<html/>')).transcribe(self.audio)

        self.assertIn('没有返回 JSON', str(ctx.exception))

    async def test_a_response_without_text_is_rejected(self) -> None:
        with self.assertRaises(AsrError) as ctx:
            await _client(lambda request: httpx.Response(200, json={'segments': []})).transcribe(self.audio)

        self.assertIn('缺少 text', str(ctx.exception))

    async def test_an_unreachable_endpoint_is_reported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError('connection refused', request=request)

        with self.assertRaises(AsrError) as ctx:
            await _client(handler).transcribe(self.audio)

        self.assertIn('无法连接转写服务', str(ctx.exception))


class HealthTest(unittest.IsolatedAsyncioTestCase):
    async def test_health_reports_the_loaded_backend(self) -> None:
        payload = await _client(
            lambda request: httpx.Response(200, json={'status': 'ok', 'backend': 'qwen3-1.7b'})
        ).health()

        self.assertEqual('qwen3-1.7b', payload['backend'])

    async def test_health_raises_on_a_bad_status(self) -> None:
        with self.assertRaises(AsrError):
            await _client(lambda request: httpx.Response(503, text='starting')).health()


class ClientForTest(unittest.TestCase):
    def test_the_endpoint_and_key_come_from_the_settings(self) -> None:
        settings = default_media_settings()
        settings['asr_api_key'] = 'sk-test'

        client = client_for(settings, 'accurate')

        self.assertEqual('http://192.168.130.202:8317/v1', client.endpoint)
        self.assertEqual('sk-test', client._api_key)

    def test_a_tier_without_an_endpoint_is_a_settings_error(self) -> None:
        settings = default_media_settings()
        settings['asr_endpoints'] = {'accurate': '', 'fast': ''}

        with self.assertRaises(ValueError):
            client_for(settings, 'accurate')


if __name__ == '__main__':
    unittest.main()
