"""Chunking rules for the ASR upload.

The interesting behaviour is what must *not* happen: a short trailing chunk going
out on its own (the decoder answers that with garbage), and a chunk that grows
past the ceiling the token budget depends on.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hippo.media import audio as audio_module
from hippo.media.audio import AudioError, split

_NEEDS_FFMPEG = shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None


class _FakeFfmpeg:
    """Stands in for ffmpeg: writes the files the real command would produce."""

    def __init__(self, workdir: Path, durations: list[float]) -> None:
        self.workdir = workdir
        self.durations = durations
        self.calls: list[list[str]] = []

    def __call__(self, args) -> None:
        self.calls.append(list(args))
        if '-f' in args and 'segment' in args:
            for index, _ in enumerate(self.durations):
                (self.workdir / f'part-{index:03d}.mp3').write_bytes(b'x')
            return
        if '-f' in args and 'concat' in args:
            (self.workdir / 'tail.mp3').write_bytes(b'x')
            return
        raise AssertionError(f'unexpected ffmpeg call: {args}')


def _fake_probe(workdir: Path, durations: list[float]):
    table = {workdir / f'part-{index:03d}.mp3': duration for index, duration in enumerate(durations)}
    table[workdir / 'tail.mp3'] = sum(durations[-2:])

    def probe(path: Path) -> float:
        return table[path]

    return probe


class SplitTest(unittest.TestCase):
    def _split(self, durations: list[float], **overrides) -> list:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            params = {'chunk_seconds': 300, 'min_tail_seconds': 60, 'max_chunk_seconds': 500}
            params.update(overrides)
            with (
                patch.object(audio_module, '_run_ffmpeg', new=_FakeFfmpeg(workdir, durations)),
                patch.object(audio_module, 'probe_duration', new=_fake_probe(workdir, durations)),
            ):
                return split(workdir / 'audio.mp3', workdir, **params)

    def test_a_healthy_tail_is_kept_as_its_own_chunk(self) -> None:
        chunks = self._split([300.0, 300.0, 120.0])

        self.assertEqual(3, len(chunks))
        self.assertEqual([0.0, 300.0, 600.0], [chunk.start_seconds for chunk in chunks])

    def test_a_short_tail_is_joined_onto_the_previous_chunk(self) -> None:
        chunks = self._split([300.0, 300.0, 20.0])

        self.assertEqual(2, len(chunks))
        self.assertEqual([0.0, 300.0], [chunk.start_seconds for chunk in chunks])
        self.assertEqual(320.0, chunks[1].duration_seconds)

    def test_a_single_chunk_needs_no_join(self) -> None:
        chunks = self._split([20.0])

        self.assertEqual(1, len(chunks))
        self.assertEqual(20.0, chunks[0].duration_seconds)

    def test_a_chunk_over_the_ceiling_is_refused(self) -> None:
        with self.assertRaises(AudioError) as ctx:
            self._split([300.0, 300.0], max_chunk_seconds=200)

        self.assertIn('超过上限', str(ctx.exception))

    def test_no_parts_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            with (
                patch.object(audio_module, '_run_ffmpeg', new=lambda args: None),
                self.assertRaises(AudioError),
            ):
                split(workdir / 'audio.mp3', workdir, chunk_seconds=300, min_tail_seconds=60, max_chunk_seconds=500)


@unittest.skipUnless(_NEEDS_FFMPEG, 'needs ffmpeg')
class SplitIntegrationTest(unittest.TestCase):
    """One pass over real ffmpeg, so the command lines themselves are covered."""

    @staticmethod
    def _tone(path: Path, seconds: int) -> None:
        subprocess.run(
            [
                'ffmpeg',
                '-y',
                '-v',
                'error',
                '-f',
                'lavfi',
                '-i',
                f'sine=frequency=440:duration={seconds}',
                '-ac',
                '1',
                '-ar',
                '16000',
                '-c:a',
                'libmp3lame',
                '-b:a',
                '64k',
                str(path),
            ],
            check=True,
            capture_output=True,
        )

    def test_twelve_seconds_in_five_second_chunks_folds_the_two_second_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            self._tone(workdir / 'tone.mp3', 12)

            chunks = split(
                workdir / 'tone.mp3',
                workdir,
                chunk_seconds=5,
                min_tail_seconds=3,
                max_chunk_seconds=20,
            )

            self.assertEqual(2, len(chunks))
            self.assertAlmostEqual(0.0, chunks[0].start_seconds, places=2)
            # 偏移来自实测切点，跟着 mp3 帧边界走，不是名义上的 5.000s。
            self.assertEqual(chunks[0].duration_seconds, chunks[1].start_seconds)
            self.assertAlmostEqual(5.0, chunks[1].start_seconds, places=1)
            self.assertAlmostEqual(12.0, sum(chunk.duration_seconds for chunk in chunks), places=1)

    def test_transcode_produces_the_mono_low_rate_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            self._tone(workdir / 'tone.mp3', 3)
            target = workdir / 'audio.mp3'

            audio_module.transcode(workdir / 'tone.mp3', target)

            self.assertTrue(target.exists())
            self.assertAlmostEqual(3.0, audio_module.probe_duration(target), places=1)

    def test_archive_original_copies_without_re_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            self._tone(workdir / 'tone.mp3', 2)
            target = workdir / 'kept.mp3'

            audio_module.archive(workdir / 'tone.mp3', target, bitrate='original')

            self.assertEqual(
                (workdir / 'tone.mp3').read_bytes(),
                target.read_bytes(),
            )


if __name__ == '__main__':
    unittest.main()
