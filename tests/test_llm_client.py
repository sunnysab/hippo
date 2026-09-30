"""LLM error classification.

Every SDK failure must come out as a Chinese message and a status a client can
act on; leaking a raw exception is what produces a 500 with a stack trace.
"""

import unittest
from unittest.mock import AsyncMock, patch

import httpx
import openai

from hippo.api.routers import llm as llm_api
from hippo.exceptions import ApiError
from hippo.llm import client as llm_client

_REQUEST = httpx.Request('POST', 'http://llm.local/v1/chat/completions')


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=_REQUEST, json={'error': 'nope'})


class ClassifyTest(unittest.TestCase):
    def test_authentication_failure_says_check_the_key(self) -> None:
        message, status = llm_client._classify(openai.AuthenticationError('bad key', response=_response(401), body=None))
        self.assertIn('API Key', message)
        self.assertEqual(502, status)

    def test_rate_limit_maps_to_429(self) -> None:
        _, status = llm_client._classify(openai.RateLimitError('slow down', response=_response(429), body=None))
        self.assertEqual(429, status)

    def test_timeout_maps_to_504(self) -> None:
        message, status = llm_client._classify(openai.APITimeoutError(request=_REQUEST))
        self.assertIn('超时', message)
        self.assertEqual(504, status)

    def test_connection_error_names_the_base_url(self) -> None:
        message, _ = llm_client._classify(openai.APIConnectionError(request=_REQUEST))
        self.assertIn('base_url', message)

    def test_unknown_model_says_so(self) -> None:
        message, _ = llm_client._classify(openai.NotFoundError('nope', response=_response(404), body=None))
        self.assertIn('模型', message)

    def test_an_unexpected_error_still_gets_a_message(self) -> None:
        message, status = llm_client._classify(RuntimeError('boom'))
        self.assertTrue(message)
        self.assertEqual(502, status)


class ResolveProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_missing_provider_is_a_503_not_a_crash(self) -> None:
        storage = SimpleNamespaceStorage(default=None)
        with self.assertRaises(ApiError) as ctx:
            await llm_client.resolve_provider(storage)
        self.assertEqual(503, ctx.exception.status)

    async def test_the_default_is_used_when_no_id_is_given(self) -> None:
        default = {'id': 2, 'name': 'd', 'model': 'm', 'base_url': 'http://x', 'api_key': 'k'}
        storage = SimpleNamespaceStorage(default=default)
        self.assertEqual(default, await llm_client.resolve_provider(storage))

    async def test_a_disabled_explicit_provider_falls_back_to_the_default(self) -> None:
        default = {'id': 2, 'name': 'd', 'model': 'm', 'base_url': 'http://x', 'api_key': 'k'}
        storage = SimpleNamespaceStorage(default=default, full={'id': 1, 'enabled': False})
        self.assertEqual(default, await llm_client.resolve_provider(storage, provider_id=1))


class SimpleNamespaceStorage:
    """Minimal stand-in exposing the two provider lookups the resolver needs."""

    def __init__(self, *, default: dict | None, full: dict | None = None) -> None:
        self.llm = _LlmStub(default=default, full=full)


class _LlmStub:
    def __init__(self, *, default: dict | None, full: dict | None) -> None:
        self._default = default
        self._full = full

    async def get_default(self) -> dict | None:
        return self._default

    async def get_full(self, provider_id: int) -> dict | None:
        return self._full


class ProbeTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_model_list_is_parsed_from_the_models_endpoint(self) -> None:
        response = httpx.Response(
            200,
            request=_REQUEST,
            json={'data': [{'id': 'qwen'}, {'id': 'llama'}]},
        )
        self.assertEqual(['qwen', 'llama'], llm_api._model_ids(response))

    async def test_a_non_json_body_does_not_raise(self) -> None:
        response = httpx.Response(200, request=_REQUEST, text='<html>not json</html>')
        self.assertEqual([], llm_api._model_ids(response))

    async def test_an_unreachable_provider_reports_ok_false(self) -> None:
        storage = _storage_for_probe()
        with patch('hippo.api.routers.llm.httpx.AsyncClient') as client_cls:
            client = client_cls.return_value.__aenter__.return_value
            client.get = AsyncMock(side_effect=httpx.ConnectError('refused', request=_REQUEST))
            result = await llm_api.test_provider('1', _=object(), storage=storage)

        self.assertFalse(result['ok'])
        self.assertIn('无法连接', result['error'])


def _storage_for_probe():
    class _Storage:
        llm = None

    storage = _Storage()
    storage.llm = _ProviderStub()
    return storage


class _ProviderStub:
    async def get_full(self, provider_id: int) -> dict:
        return {
            'id': 1,
            'name': 'p',
            'base_url': 'http://llm.local/v1',
            'api_key': 'k',
            'model': 'm',
        }


if __name__ == '__main__':
    unittest.main()
