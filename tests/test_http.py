import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.http import MPClient
from hippo.sync_service import _run_backfill_images


class MPClientTlsFallbackTest(unittest.TestCase):
    def test_mpclient_retries_with_certifi_when_system_ca_bundle_is_missing(self) -> None:
        calls: list[dict] = []
        fallback_client = SimpleNamespace(is_closed=False, aclose=AsyncMock())

        def fake_async_client(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise FileNotFoundError(2, 'No such file or directory')
            return fallback_client

        with (
            patch('hippo.http.httpx.AsyncClient', side_effect=fake_async_client),
            patch('hippo.http.certifi.where', return_value='/tmp/certifi-cacert.pem'),
        ):
            client = MPClient(article_worker=None, article_worker_proxy=None)

        self.assertIs(client.client, fallback_client)
        self.assertEqual(len(calls), 2)
        self.assertNotIn('verify', calls[0])
        self.assertEqual(calls[1]['verify'], '/tmp/certifi-cacert.pem')


class MPClientProxyTest(unittest.TestCase):
    def _patch_httpx(self):
        client_calls: list[dict] = []
        transport_calls: list[dict] = []

        def fake_async_client(*args, **kwargs):
            client_calls.append(kwargs)
            return SimpleNamespace(is_closed=False, aclose=AsyncMock())

        def fake_transport(*args, **kwargs):
            transport_calls.append(kwargs)
            return SimpleNamespace(aclose=AsyncMock())

        return (
            patch('hippo.http.httpx.AsyncClient', side_effect=fake_async_client),
            patch('hippo.http.httpx.AsyncHTTPTransport', side_effect=fake_transport),
            client_calls,
            transport_calls,
        )

    def test_mpclient_passes_generic_proxy_to_primary_client(self) -> None:
        proxy = 'http://proxy.example:8888'
        async_client_patch, transport_patch, client_calls, _ = self._patch_httpx()
        with async_client_patch, transport_patch:
            MPClient(proxy=proxy, article_worker=None, article_worker_proxy=None)

        self.assertEqual(proxy, client_calls[0]['proxy'])

    def test_article_client_inherits_generic_proxy(self) -> None:
        proxy = 'http://proxy.example:8888'
        async_client_patch, transport_patch, _, transport_calls = self._patch_httpx()
        with async_client_patch, transport_patch:
            MPClient(proxy=proxy, article_worker='https://worker.example', article_worker_proxy=None)

        self.assertEqual(proxy, transport_calls[0]['proxy'])

    def test_article_proxy_overrides_generic_proxy(self) -> None:
        generic_proxy = 'http://generic-proxy.example:8888'
        article_proxy = 'http://article-proxy.example:8888'
        async_client_patch, transport_patch, client_calls, transport_calls = self._patch_httpx()
        with async_client_patch, transport_patch:
            MPClient(
                proxy=generic_proxy,
                article_worker='https://worker.example',
                article_worker_proxy=article_proxy,
            )

        self.assertEqual(generic_proxy, client_calls[0]['proxy'])
        self.assertEqual(article_proxy, transport_calls[0]['proxy'])

    def test_mpclient_without_proxy_preserves_existing_client_shape(self) -> None:
        async_client_patch, transport_patch, client_calls, transport_calls = self._patch_httpx()
        with async_client_patch, transport_patch:
            client = MPClient(proxy=None, article_worker=None, article_worker_proxy=None)

        self.assertIsNone(client.article_client)
        self.assertEqual(1, len(client_calls))
        self.assertNotIn('proxy', client_calls[0])
        self.assertEqual([], transport_calls)

    def test_mpclient_does_not_log_proxy_credentials(self) -> None:
        proxy = 'http://generic-user:generic-secret@generic-proxy.example:8888'
        article_proxy = 'http://article-user:article-secret@article-proxy.example:8888'
        async_client_patch, transport_patch, _, _ = self._patch_httpx()
        with (
            async_client_patch,
            transport_patch,
            patch('hippo.http.logger.debug') as debug,
        ):
            MPClient(proxy=proxy, article_worker='https://worker.example', article_worker_proxy=article_proxy)

        rendered_arguments = repr(debug.call_args_list)
        for sensitive_value in (
            'generic-user',
            'generic-secret',
            'generic-proxy.example',
            'article-user',
            'article-secret',
            'article-proxy.example',
        ):
            self.assertNotIn(sensitive_value, rendered_arguments)


class BackfillLoggingTest(unittest.TestCase):
    def test_run_backfill_images_logs_and_swallows_failures(self) -> None:
        with (
            patch(
                'hippo.cli._backfill_article_images_async',
                new=AsyncMock(side_effect=FileNotFoundError(2, 'No such file or directory')),
            ),
            patch('hippo.sync_service.logger.exception') as logger_exception,
        ):
            asyncio.run(_run_backfill_images())

        logger_exception.assert_called_once()


if __name__ == '__main__':
    unittest.main()
