from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hippo.container import AppContainer, build_downloader_container, build_sync_container
from hippo.downloader import ArticleDownloader
from hippo.sources import SourceError
from hippo.sources.podcast import FEED_SOURCE
from hippo.weixin_daemon import SessionExpiredError


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


class BuildSyncContainerSourcesTest(unittest.TestCase):
    """Only the sources a run selected may be built or registered."""

    def _build(self, *, sources, media_settings=None, media_adapters=None, **kwargs):
        with (
            patch('hippo.container.MPClient'),
            patch('hippo.container.ArticleDownloader'),
            patch('hippo.container.MediaSources') as media_sources,
            patch('hippo.container.WeixinSource') as weixin_source,
            patch('hippo.container.WeixinArticleSync') as weixin_sync,
        ):
            media_sources.return_value.adapters = dict(media_adapters or {})
            container = build_sync_container(
                storage=SimpleNamespace(),
                enable_download=False,
                enable_images=False,
                media_settings=media_settings,
                sources=sources,
                **kwargs,
            )
        return container, weixin_source, weixin_sync

    def test_a_run_without_weixin_never_builds_the_daemon_client(self) -> None:
        container, weixin_source, weixin_sync = self._build(
            sources={'youtube'},
            media_adapters={'youtube': object()},
        )

        weixin_source.assert_not_called()
        weixin_sync.assert_not_called()
        self.assertNotIn('weixin', container.adapters)
        self.assertIn('youtube', container.adapters)

    def test_a_mixed_run_still_builds_weixin(self) -> None:
        container, weixin_source, _ = self._build(
            sources={'weixin', 'youtube'},
            media_adapters={'youtube': object()},
        )

        weixin_source.assert_called_once()
        self.assertIn('weixin', container.adapters)

    def test_a_text_feed_is_registered_even_when_transcription_is_off(self) -> None:
        feed_adapter = object()
        container, _, _ = self._build(
            sources={'feed', 'weixin'},
            media_settings={'enabled': False},
            media_adapters={'bilibili': object(), 'youtube': object(), 'podcast': object(), FEED_SOURCE: feed_adapter},
        )

        # The RSS/text source does not need ASR, so the switch must not drop it.
        self.assertIs(container.adapters.get(FEED_SOURCE), feed_adapter)
        # The audio/video sources stay off and their accounts are reported unsupported.
        self.assertNotIn('podcast', container.adapters)
        self.assertNotIn('bilibili', container.adapters)
        self.assertNotIn('youtube', container.adapters)

    def test_media_adapters_are_registered_when_transcription_is_on(self) -> None:
        candidates = {'bilibili': object(), 'youtube': object(), 'podcast': object(), FEED_SOURCE: object()}
        container, _, _ = self._build(
            sources={'weixin', 'youtube', FEED_SOURCE},
            media_settings={'enabled': True},
            media_adapters=candidates,
        )

        self.assertEqual(set(container.adapters) - {'weixin'}, set(candidates))


class _RecordingContext:
    """Async context recording how often it was entered and exited."""

    def __init__(self, *, fail: BaseException | None = None) -> None:
        self.entered = 0
        self.exited = 0
        self.fail = fail

    async def __aenter__(self) -> _RecordingContext:
        self.entered += 1
        if self.fail is not None:
            raise self.fail
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.exited += 1


class _RecordingCloser:
    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


def _container(
    *,
    client: _RecordingContext | None = None,
    downloader: _RecordingContext | None = None,
    weixin_source: _RecordingContext | None = None,
    media_sources: _RecordingCloser | None = None,
    adapters: dict | None = None,
) -> AppContainer:
    return AppContainer(
        storage=SimpleNamespace(),
        client=client or _RecordingContext(),
        image_service=None,
        downloader=downloader,
        weixin_source=weixin_source,
        adapters=adapters if adapters is not None else {},
        media_sources=media_sources,
    )


class ContainerLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_weixin_session_failure_is_isolated_and_the_half_open_source_is_closed(self) -> None:
        client = _RecordingContext()
        weixin = _RecordingContext(fail=SessionExpiredError('daemon not logged in'))
        container = _container(client=client, weixin_source=weixin, adapters={'weixin': object()})

        app = await container.__aenter__()

        self.assertEqual(weixin.exited, 1)
        self.assertIsNone(app.weixin_source)
        self.assertEqual(app.weixin_unavailable, 'daemon not logged in')
        with self.assertRaises(SourceError) as caught:
            await app.adapters['weixin'].sync_account(biz='biz', source_key='key')
        self.assertIn('weixin source unavailable', str(caught.exception))
        await app.__aexit__(None, None, None)
        self.assertEqual(client.exited, 1)

    async def test_a_weixin_connect_failure_is_isolated(self) -> None:
        weixin = _RecordingContext(fail=ConnectionRefusedError('no daemon socket'))
        container = _container(weixin_source=weixin)

        app = await container.__aenter__()

        self.assertIsNotNone(app.weixin_unavailable)
        self.assertIsNone(app.weixin_source)
        self.assertEqual(weixin.exited, 1)
        await app.__aexit__(None, None, None)

    async def test_an_unexpected_weixin_failure_propagates_and_releases_the_client(self) -> None:
        client = _RecordingContext()
        weixin = _RecordingContext(fail=ValueError('programming bug'))
        container = _container(client=client, weixin_source=weixin)

        with self.assertRaises(ValueError):
            await container.__aenter__()

        # The half-open source and the already-opened client are both released.
        self.assertEqual(weixin.exited, 1)
        self.assertEqual(client.exited, 1)

    async def test_cancellation_during_weixin_open_propagates_and_releases_the_client(self) -> None:
        client = _RecordingContext()
        weixin = _RecordingContext(fail=asyncio.CancelledError())
        container = _container(client=client, weixin_source=weixin)

        with self.assertRaises(asyncio.CancelledError):
            await container.__aenter__()

        self.assertEqual(weixin.exited, 1)
        self.assertEqual(client.exited, 1)

    async def test_a_downloader_start_failure_releases_the_client(self) -> None:
        client = _RecordingContext()
        downloader = _RecordingContext(fail=RuntimeError('no image store'))
        container = _container(client=client, downloader=downloader)

        with self.assertRaises(RuntimeError):
            await container.__aenter__()

        self.assertEqual(client.exited, 1)
        # The downloader never finished entering, so it is not closed again.
        self.assertEqual(downloader.exited, 0)

    async def test_a_downloader_failure_after_weixin_closes_the_weixin_source(self) -> None:
        client = _RecordingContext()
        weixin = _RecordingContext()
        downloader = _RecordingContext(fail=RuntimeError('no image store'))
        container = _container(client=client, downloader=downloader, weixin_source=weixin)

        with self.assertRaises(RuntimeError):
            await container.__aenter__()

        self.assertEqual(weixin.exited, 1)
        self.assertEqual(client.exited, 1)

    async def test_a_second_exit_does_not_close_anything_twice(self) -> None:
        client = _RecordingContext()
        media = _RecordingCloser()
        container = _container(client=client, media_sources=media)

        app = await container.__aenter__()
        await app.__aexit__(None, None, None)
        await app.__aexit__(None, None, None)

        self.assertEqual(client.exited, 1)
        self.assertEqual(media.closed, 1)


class RunSyncJobSourceTest(unittest.IsolatedAsyncioTestCase):
    """``run_sync_job`` tells the container which sources the accounts cover."""

    async def _run(self, accounts) -> MagicMock:
        from hippo import sync_service

        storage = SimpleNamespace(
            accounts=SimpleNamespace(list_syncable_accounts=AsyncMock(return_value=accounts)),
            rollback=AsyncMock(),
        )

        class _StorageContext:
            async def __aenter__(self):
                return storage

            async def __aexit__(self, exc_type, exc, tb):
                return None

        container = SimpleNamespace(adapters={}, downloader=None, weixin_unavailable=None)
        container.__aenter__ = AsyncMock(return_value=container)
        container.__aexit__ = AsyncMock(return_value=None)

        with (
            patch.object(sync_service, 'open_storage', return_value=_StorageContext()),
            patch.object(sync_service, 'get_sync_settings', AsyncMock(return_value={'download_content': False})),
            patch.object(sync_service, 'set_sync_state', AsyncMock()),
            patch.object(sync_service, 'get_media_settings', AsyncMock(return_value={})),
            patch.object(sync_service, 'build_sync_container', MagicMock(return_value=container)) as build,
            patch.object(sync_service, 'ArticleSyncService') as service,
            patch.object(sync_service, '_persist_sync_outcome', AsyncMock(return_value={'status': 'success'})),
        ):
            service.return_value.sync_accounts = AsyncMock(return_value=MagicMock())
            await sync_service.run_sync_job()
            return build

    async def test_selected_sources_are_forwarded_to_the_container(self) -> None:
        build = await self._run(
            [SimpleNamespace(biz='wx', source='weixin'), SimpleNamespace(biz='yt', source='youtube')]
        )

        self.assertEqual(build.call_args.kwargs['sources'], {'weixin', 'youtube'})

    async def test_a_media_only_run_excludes_weixin(self) -> None:
        build = await self._run([SimpleNamespace(biz='yt', source='youtube')])

        self.assertEqual(build.call_args.kwargs['sources'], {'youtube'})


if __name__ == '__main__':
    unittest.main()
