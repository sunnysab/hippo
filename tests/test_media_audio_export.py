"""Lifting an audio track out of an archived video, on request.

Two things matter here: the state the reader polls must say what is really going
on (a restarted process leaves a `pending` row behind and nothing to wait for),
and pressing the button twice must not start two extractions.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.media import audio_export


def _storage(audio=None, video=None) -> SimpleNamespace:
    assets: dict[str, dict | None] = {'audio': audio, 'video': video}

    async def get(article_pk, *, kind='audio'):
        del article_pk
        return assets[kind]

    storage = SimpleNamespace(
        media_assets=SimpleNamespace(
            get=AsyncMock(side_effect=get),
            save=AsyncMock(),
            mark_pending=AsyncMock(),
            mark_failed=AsyncMock(),
        ),
        commit=AsyncMock(),
    )
    return storage


_STORED_VIDEO = {'state': 'stored', 's3_key': 'media/video/7.mp4', 'size_bytes': 4096}
_STORED_AUDIO = {'state': 'stored', 's3_key': 'media/audio/7.mp3', 'size_bytes': 512}


class AudioStateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        audio_export._running.clear()

    async def test_a_stored_copy_is_ready(self) -> None:
        state = await audio_export.audio_state(_storage(audio=_STORED_AUDIO, video=_STORED_VIDEO), 7)

        self.assertEqual('stored', state)

    async def test_without_a_kept_video_there_is_nothing_to_lift(self) -> None:
        self.assertEqual('unavailable', await audio_export.audio_state(_storage(), 7))
        # 只有音频行的失败记录、没有视频，同样抽不出来。
        self.assertEqual(
            'unavailable',
            await audio_export.audio_state(_storage(audio={'state': 'failed'}), 7),
        )

    async def test_a_pending_row_this_process_does_not_hold_is_not_waited_on(self) -> None:
        # 服务重启后任务已经不在了，继续报 pending 会让读者无限等下去：可以再抽一次。
        self.assertEqual(
            'missing',
            await audio_export.audio_state(_storage(audio={'state': 'pending'}, video=_STORED_VIDEO), 7),
        )

    async def test_a_running_extraction_reports_pending(self) -> None:
        running = asyncio.get_running_loop().create_future()
        audio_export._running[7] = running  # type: ignore[assignment]

        self.assertEqual(
            'pending',
            await audio_export.audio_state(_storage(audio={'state': 'pending'}, video=_STORED_VIDEO), 7),
        )


class EnsureAudioTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        audio_export._running.clear()

    def _patched(self):
        def _fetch(key, target, **_kwargs):
            del key
            target.write_bytes(b'v' * 64)
            return 64

        def _archive(source, target, **kwargs):
            del source, kwargs
            target.write_bytes(b'a' * 32)

        return (
            patch('hippo.media.audio_export.archive_module.fetch_object', new=_fetch),
            patch('hippo.media.audio_export.audio_module.archive', new=_archive),
            patch(
                'hippo.media.audio_export.archive_module.store_media',
                new=lambda **kwargs: ('media/audio/7.mp3', kwargs['path'].stat().st_size),
            ),
        )

    def _open_storage(self, storage):
        # 抽取是脱离请求跑的，它自己开连接；测试里换成同一个假仓储。
        class _Ctx:
            async def __aenter__(self):
                return storage

            async def __aexit__(self, *exc):
                return False

        return patch('hippo.media.audio_export.open_storage', new=lambda: _Ctx())

    async def test_it_extracts_stores_and_commits(self) -> None:
        storage = _storage(video=_STORED_VIDEO)
        fetch, archive, store = self._patched()
        with fetch, archive, store, self._open_storage(storage):
            state = await audio_export.ensure_audio(storage, 7)
            self.assertEqual('pending', state)
            storage.media_assets.mark_pending.assert_awaited_once_with(7)
            # 任务在同一个补丁里跑完：它自己开连接，离开补丁后就落到真实库上了。
            for task in list(audio_export._running.values()):
                await task

        saved = storage.media_assets.save.await_args.kwargs
        self.assertEqual('audio/mpeg', saved['content_type'])
        self.assertEqual('media/audio/7.mp3', saved['s3_key'])
        # 抽出来的音频没有自己的原始地址：留空才不会把它 302 到文章页。
        self.assertIsNone(saved['orig_url'])
        self.assertTrue(storage.commit.await_count)
        self.assertEqual({}, audio_export._running)

    async def test_a_second_request_joins_the_running_extraction(self) -> None:
        storage = _storage(video=_STORED_VIDEO)
        fetch, archive, store = self._patched()
        with fetch, archive, store, self._open_storage(storage):
            self.assertEqual('pending', await audio_export.ensure_audio(storage, 7))
            self.assertEqual('pending', await audio_export.ensure_audio(storage, 7))
            self.assertEqual(1, len(audio_export._running))
            for task in list(audio_export._running.values()):
                await task

        self.assertEqual(1, storage.media_assets.mark_pending.await_count)

    async def test_a_failure_is_recorded_instead_of_being_lost(self) -> None:
        storage = _storage(video=_STORED_VIDEO)

        def _boom(*args, **kwargs):
            raise RuntimeError('ffmpeg 处理失败')

        with (
            patch('hippo.media.audio_export.archive_module.fetch_object', new=lambda *a, **k: 0),
            patch('hippo.media.audio_export.audio_module.archive', new=_boom),
            self._open_storage(storage),
        ):
            await audio_export.ensure_audio(storage, 7)
            for task in list(audio_export._running.values()):
                await task

        self.assertEqual('ffmpeg 处理失败', storage.media_assets.mark_failed.await_args.kwargs['error'])
        self.assertEqual({}, audio_export._running)

    async def test_a_stored_copy_starts_nothing(self) -> None:
        storage = _storage(audio=_STORED_AUDIO, video=_STORED_VIDEO)

        self.assertEqual('stored', await audio_export.ensure_audio(storage, 7))
        self.assertEqual({}, audio_export._running)
        storage.media_assets.mark_pending.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
