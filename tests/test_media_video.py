"""Merging a video track with its audio, with ffmpeg faked out and then for real.

What is worth pinning down: the command line protects both inputs and never uses a
shell, a size ceiling cannot succeed by truncating, every failure path removes its
partial output, and a real local merge really does land pictures and sound in one
MP4.

The mocked groups never touch the machine's tools, so they run anywhere; only the
integration group needs the real binaries and skips without them.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hippo.media import video as video_module
from hippo.media.video import MERGE_TIMEOUT_SECONDS, VideoError, merge

_SIZE = 64

_TOOLS = ('ffmpeg', 'ffprobe', 'prlimit')
_NEEDS_TOOLS = all(shutil.which(tool) for tool in _TOOLS)

#: Producers run on tiny lavfi inputs, so anything near this is a hang, not work.
_FIXTURE_TIMEOUT_SECONDS = 60


class _FakeFfmpeg:
    """Stands in for ``subprocess.run``: records the command, writes the output."""

    def __init__(self, *, returncode: int = 0, output: bytes | None = b'x' * _SIZE, error: Exception | None = None):
        self.returncode = returncode
        self.output = output
        self.error = error
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        self.kwargs.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.output is not None:
            Path(args[-1]).write_bytes(self.output)
        return SimpleNamespace(returncode=self.returncode)


def _patch_runner(runner: _FakeFfmpeg):
    # Only `run` is replaced: DEVNULL and TimeoutExpired stay the real objects, so
    # the code under test keeps raising and catching the same types it does live.
    return patch.object(
        video_module,
        'subprocess',
        SimpleNamespace(run=runner, DEVNULL=subprocess.DEVNULL, TimeoutExpired=subprocess.TimeoutExpired),
    )


def _patch_tools(*, missing: bool = False):
    """Fake tool discovery so the mocked groups need no binaries installed."""

    def which(name: str) -> str | None:
        return None if missing else f'/usr/bin/{name}'

    return patch.object(video_module, 'shutil', SimpleNamespace(which=which))


class _MergeTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.workdir = Path(self._dir.name)
        self.video = self.workdir / 'video.mp4'
        self.audio = self.workdir / 'audio.m4a'
        self.target = self.workdir / 'merged.mp4'
        self.video.write_bytes(b'v')
        self.audio.write_bytes(b'a')
        tools = _patch_tools()
        tools.start()
        self.addCleanup(tools.stop)

    def _merge(
        self,
        runner: _FakeFfmpeg,
        *,
        max_bytes: int = 1 << 20,
        video: Path | None = None,
        audio: Path | None = None,
        target: Path | None = None,
        **kwargs,
    ) -> int:
        patched = _patch_runner(runner)
        patched.start()
        self.addCleanup(patched.stop)
        return merge(
            video if video is not None else self.video,
            audio if audio is not None else self.audio,
            target if target is not None else self.target,
            max_bytes=max_bytes,
            **kwargs,
        )

    def _assert_failed_merge(
        self,
        runner: _FakeFfmpeg,
        *,
        max_bytes: int = 1 << 20,
        target: Path | None = None,
        **kwargs,
    ) -> str:
        """A failure must raise and leave no partial output behind."""
        with self.assertRaises(VideoError) as raised:
            self._merge(runner, max_bytes=max_bytes, target=target, **kwargs)
        self.assertFalse((target if target is not None else self.target).exists())
        return str(raised.exception)


class MergeCommandTest(_MergeTestBase):
    """The command line itself: what it protects, and what it must never ask for."""

    def test_the_streams_are_copied_and_faststart_is_requested(self) -> None:
        # Re-encoding the picture would buy nothing: a merge only changes container.
        runner = _FakeFfmpeg()

        size = self._merge(runner)

        self.assertEqual(_SIZE, size)
        args = runner.calls[-1]
        self.assertEqual('copy', args[args.index('-c:v') + 1])
        self.assertEqual('copy', args[args.index('-c:a') + 1])
        self.assertEqual('+faststart', args[args.index('-movflags') + 1])
        self.assertIn('0:v:0', args)
        self.assertIn('1:a:0', args)

    def test_the_protocol_whitelist_covers_both_inputs(self) -> None:
        # `-protocol_whitelist` is an input option, so one copy protects only the -i
        # right after it. Both inputs must carry their own, in their own order.
        runner = _FakeFfmpeg()

        self._merge(runner)

        args = runner.calls[-1]
        inputs = [index for index, item in enumerate(args) if item == '-i']
        whitelists = [index for index, item in enumerate(args) if item == '-protocol_whitelist']
        self.assertEqual(2, len(inputs))
        self.assertEqual(2, len(whitelists))
        # Interleaved as whitelist, path, -i, path: each -i is preceded by exactly
        # one whitelist, and neither input is left relying on the other's.
        self.assertEqual([index - 2 for index in inputs], whitelists)
        for index in whitelists:
            self.assertEqual('file,crypto,data', args[index + 1])

    def test_the_inputs_are_absolute_local_paths_and_no_shell_is_used(self) -> None:
        runner = _FakeFfmpeg()

        self._merge(runner)

        args = runner.calls[-1]
        inputs = [Path(args[index + 1]) for index, item in enumerate(args) if item == '-i']
        self.assertEqual([self.video.resolve(), self.audio.resolve()], inputs)
        # An argv list plus -nostdin: there is no shell to interpret anything.
        self.assertIsInstance(args, list)
        self.assertIn('-nostdin', args)
        self.assertNotIn('shell', runner.kwargs[-1])
        self.assertIsNotNone(runner.kwargs[-1]['timeout'])

    def test_a_relative_path_is_resolved_before_it_reaches_ffmpeg(self) -> None:
        # The caller may hand over whatever the job holds; ffmpeg must still see a
        # path whose meaning cannot change with a working directory.
        runner = _FakeFfmpeg()
        relative_video = Path(os.path.relpath(self.video, Path.cwd()))
        relative_audio = Path(os.path.relpath(self.audio, Path.cwd()))
        relative_target = Path(os.path.relpath(self.target, Path.cwd()))
        self.assertFalse(relative_video.is_absolute())

        size = self._merge(
            runner,
            video=relative_video,
            audio=relative_audio,
            target=relative_target,
        )

        self.assertEqual(_SIZE, size)
        args = runner.calls[-1]
        inputs = [Path(args[index + 1]) for index, item in enumerate(args) if item == '-i']
        self.assertEqual([self.video.resolve(), self.audio.resolve()], inputs)
        self.assertEqual(self.target.resolve(), Path(args[-1]))

    def test_the_hard_size_ceiling_is_applied_by_prlimit(self) -> None:
        # ffmpeg's own -fs writes a bit less and still exits 0, so the kernel limit
        # is what makes the ceiling hard.
        runner = _FakeFfmpeg()

        self._merge(runner, max_bytes=4096)

        args = runner.calls[-1]
        self.assertEqual('--fsize=4096:4096', args[1])
        # A condensed writer is killed here, and its dump would only cost disk.
        self.assertIn('--core=0:0', args)
        self.assertNotIn('-fs', args)

    def test_a_previous_target_is_not_reused(self) -> None:
        self.target.write_bytes(b'stale' * 100)
        runner = _FakeFfmpeg()

        size = self._merge(runner)

        self.assertEqual(_SIZE, size)
        self.assertEqual(_SIZE, self.target.stat().st_size)


class MergeInputSafetyTest(_MergeTestBase):
    """`-y` plus the pre-flight unlink must never be pointed at an input."""

    def test_the_target_may_not_be_the_video(self) -> None:
        runner = _FakeFfmpeg()

        with self.assertRaises(VideoError) as raised:
            self._merge(runner, target=self.video)

        self.assertIn('must not be one of its inputs', str(raised.exception))
        self.assertEqual([], runner.calls)
        self.assertEqual(b'v', self.video.read_bytes())

    def test_the_target_may_not_be_the_audio(self) -> None:
        runner = _FakeFfmpeg()

        with self.assertRaises(VideoError):
            self._merge(runner, target=self.audio)

        self.assertEqual([], runner.calls)
        self.assertEqual(b'a', self.audio.read_bytes())

    def test_a_relative_alias_of_an_input_is_refused_too(self) -> None:
        runner = _FakeFfmpeg()
        alias = Path(os.path.relpath(self.video, Path.cwd()))

        with self.assertRaises(VideoError):
            self._merge(runner, target=alias)

        self.assertEqual([], runner.calls)
        self.assertEqual(b'v', self.video.read_bytes())

    def test_a_symlink_to_an_input_is_refused_too(self) -> None:
        runner = _FakeFfmpeg()
        link = self.workdir / 'link.mp4'
        link.symlink_to(self.video)

        with self.assertRaises(VideoError):
            self._merge(runner, target=link)

        self.assertEqual([], runner.calls)
        self.assertEqual(b'v', self.video.read_bytes())


class MergeFailureTest(_MergeTestBase):
    """Every way it can go wrong ends in a raised error and no partial output."""

    def test_a_non_zero_exit_is_an_error_and_the_output_is_removed(self) -> None:
        message = self._assert_failed_merge(_FakeFfmpeg(returncode=1, output=b'x' * 8))

        self.assertIn('exit 1', message)
        self.assertNotIn(str(self.workdir), message)

    def test_a_timeout_is_an_error_matching_the_wall_clock_and_the_output_is_removed(self) -> None:
        runner = _FakeFfmpeg(error=subprocess.TimeoutExpired('ffmpeg', MERGE_TIMEOUT_SECONDS))

        message = self._assert_failed_merge(runner, timeout_seconds=7.0)

        self.assertIn('7', message)

    def test_a_tool_that_cannot_be_started_is_an_error(self) -> None:
        runner = _FakeFfmpeg(error=OSError('exec format error'))

        message = self._assert_failed_merge(runner)

        # A fixed English error, with no private path in it.
        self.assertIn('could not be started', message)
        self.assertNotIn(str(self.workdir), message)

    def test_an_empty_output_is_an_error(self) -> None:
        message = self._assert_failed_merge(_FakeFfmpeg(output=b''))

        self.assertIn('empty', message)

    def test_an_output_that_hit_the_ceiling_is_not_a_success(self) -> None:
        # At the ceiling ffmpeg may still exit 0 with a slightly smaller file: only
        # the hard limit turns that into a failure, which is what this asserts.
        message = self._assert_failed_merge(_FakeFfmpeg(output=b'x' * 4096), max_bytes=4096)

        self.assertIn('ceiling', message)

    def test_a_missing_required_tool_is_an_error_not_a_weaker_merge(self) -> None:
        runner = _FakeFfmpeg()

        with (
            _patch_runner(runner),
            _patch_tools(missing=True),
            self.assertRaises(VideoError) as raised,
        ):
            merge(self.video, self.audio, self.target, max_bytes=1 << 20)

        self.assertIn('not installed', str(raised.exception))
        # Without the tool there is no quiet fallback to a merge without a ceiling.
        self.assertEqual([], runner.calls)
        self.assertFalse(self.target.exists())

    def test_a_non_positive_ceiling_is_refused(self) -> None:
        with self.assertRaises(VideoError):
            merge(self.video, self.audio, self.target, max_bytes=0)

    def test_the_binaries_own_output_is_never_captured(self) -> None:
        # Diagnostics stay out of memory and out of the error: the streams are
        # DEVNULL, so only the exit status is ever visible.
        runner = _FakeFfmpeg()

        self._merge(runner)

        kwargs = runner.kwargs[-1]
        self.assertIs(subprocess.DEVNULL, kwargs['stdout'])
        self.assertIs(subprocess.DEVNULL, kwargs['stderr'])
        self.assertNotIn('capture_output', kwargs)
        self.assertNotIn('text', kwargs)


@unittest.skipUnless(_NEEDS_TOOLS, 'needs ffmpeg, ffprobe and prlimit')
class RealMergeIntegrationTest(unittest.TestCase):
    """One merge over real ffmpeg, so the command line is proven, not described."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.workdir = Path(self._dir.name)

    @staticmethod
    def _ffmpeg(*args: str) -> None:
        subprocess.run(
            ['ffmpeg', '-y', '-v', 'error', *args],
            check=True,
            capture_output=True,
            timeout=_FIXTURE_TIMEOUT_SECONDS,
        )

    def _silent_video(self) -> Path:
        path = self.workdir / 'video.mp4'
        self._ffmpeg(
            '-f',
            'lavfi',
            '-i',
            'testsrc=size=64x64:rate=10:duration=1',
            '-c:v',
            'libx264',
            '-pix_fmt',
            'yuv420p',
            '-an',
            str(path),
        )
        return path

    def _audio(self) -> Path:
        path = self.workdir / 'audio.m4a'
        self._ffmpeg(
            '-f',
            'lavfi',
            '-i',
            'sine=frequency=440:duration=1',
            '-c:a',
            'aac',
            '-b:a',
            '64k',
            str(path),
        )
        return path

    @staticmethod
    def _stream_types(path: Path) -> list[str]:
        completed = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=_FIXTURE_TIMEOUT_SECONDS,
        )
        return [line.strip() for line in completed.stdout.splitlines() if line.strip()]

    def test_a_short_local_pair_merges_into_one_mp4_with_both_streams(self) -> None:
        target = self.workdir / 'merged.mp4'

        size = merge(self._silent_video(), self._audio(), target, max_bytes=1 << 20, timeout_seconds=120)

        self.assertTrue(target.exists())
        self.assertEqual(target.stat().st_size, size)
        self.assertLess(size, 1 << 20)
        self.assertEqual(['video', 'audio'], self._stream_types(target))
        # faststart: moov must precede mdat, or playback has to wait for the whole
        # file before it can seek.
        with target.open('rb') as handle:
            head = handle.read(1 << 16)
        self.assertLess(head.index(b'moov'), head.index(b'mdat'))

    def test_a_ceiling_below_the_result_fails_and_leaves_nothing(self) -> None:
        target = self.workdir / 'merged.mp4'

        with self.assertRaises(VideoError):
            merge(self._silent_video(), self._audio(), target, max_bytes=1024, timeout_seconds=120)

        self.assertFalse(target.exists())

    def test_a_target_that_is_an_input_keeps_the_input(self) -> None:
        video = self._silent_video()
        before = video.read_bytes()

        with self.assertRaises(VideoError):
            merge(video, self._audio(), video, max_bytes=1 << 20, timeout_seconds=120)

        self.assertEqual(before, video.read_bytes())


if __name__ == '__main__':
    unittest.main()
