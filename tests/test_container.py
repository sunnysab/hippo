from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hippo.container import build_downloader_container
from hippo.downloader import ArticleDownloader


class DownloaderContainerTest(unittest.TestCase):
    def test_omitted_article_options_preserve_mpclient_defaults(self) -> None:
        with (
            patch('hippo.container.MPClient') as mp_client,
            patch('hippo.container.ArticleDownloader'),
        ):
            build_downloader_container(storage=SimpleNamespace(), enable_images=False)

        mp_client.assert_called_once_with()

    def test_explicit_article_options_are_forwarded(self) -> None:
        with (
            patch('hippo.container.MPClient') as mp_client,
            patch('hippo.container.ArticleDownloader'),
        ):
            build_downloader_container(
                storage=SimpleNamespace(),
                enable_images=False,
                article_worker='https://worker.example',
                article_worker_proxy='http://proxy.example:8888',
                article_max_connections=4,
            )

        mp_client.assert_called_once_with(
            article_worker='https://worker.example',
            article_worker_proxy='http://proxy.example:8888',
            article_max_connections=4,
        )

    def test_direct_downloader_preserves_mpclient_defaults(self) -> None:
        with patch('hippo.downloader.MPClient') as mp_client:
            ArticleDownloader(storage=SimpleNamespace(), enable_image_worker=False)

        mp_client.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
