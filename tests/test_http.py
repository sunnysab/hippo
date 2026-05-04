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


class BackfillLoggingTest(unittest.TestCase):
    def test_run_backfill_images_logs_and_swallows_failures(self) -> None:
        with (
            patch(
                'hippo.cli._backfill_article_images_async',
                new=AsyncMock(side_effect=FileNotFoundError(2, 'No such file or directory')),
            ),
            patch('hippo.sync_service._logger.exception') as logger_exception,
        ):
            asyncio.run(_run_backfill_images())

        logger_exception.assert_called_once()


if __name__ == '__main__':
    unittest.main()
