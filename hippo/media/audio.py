"""Turning a downloaded audio file into ASR-sized chunks.

Two constraints measured against the real backend shape this module (PLAN §5.3):

* **The chunk length stays constant.** Every request then carries the same prompt
  length, which is what the XPU decoder needs to reuse its captured graph, and it
  keeps a chunk well inside the model's generation budget.
* **A very short trailing chunk is never sent.** A 20 s tail made the decoder loop
  and emit 2048 ``!`` characters, so a short remainder is joined onto the previous
  chunk instead. ``max_chunk_seconds`` is kept above ``chunk_seconds +
  min_tail_seconds`` by the settings, which is what makes the join always fit.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

#: What the ASR is fed: 16 kHz mono is what the backends resample to anyway, and
#: 64 kbps keeps a 300 s chunk near 2.4 MB.
ASR_SAMPLE_RATE = 16000
ASR_BITRATE = '64k'

#: Name of the transcoded intermediate inside the job's working directory.
AUDIO_NAME = 'audio.mp3'
_PART_PATTERN = 'part-%03d.mp3'


class AudioError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Chunk:
    """One upload-sized piece of audio, and where it sits in the original."""

    index: int
    path: Path
    start_seconds: float
    duration_seconds: float


def _run_ffmpeg(args: Sequence[str]) -> None:
    try:
        completed = subprocess.run(list(args), capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise AudioError('未找到 ffmpeg，请先安装并加入 PATH') from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or '').strip()[-800:]
        raise AudioError(f'ffmpeg 处理失败: {detail}')


def probe_duration(path: Path) -> float:
    """Duration in seconds, as ffprobe reports it."""
    args = [
        'ffprobe',
        '-v',
        'error',
        '-show_entries',
        'format=duration',
        '-of',
        'default=noprint_wrappers=1:nokey=1',
        str(path),
    ]
    try:
        completed = subprocess.run(args, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise AudioError('未找到 ffprobe，请先安装 ffmpeg') from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or '').strip()[-400:]
        raise AudioError(f'ffprobe 读取时长失败: {detail}') from exc
    try:
        return max(float(completed.stdout.strip()), 0.0)
    except ValueError as exc:
        raise AudioError(f'ffprobe 返回了无法解析的时长: {completed.stdout.strip()!r}') from exc


def transcode(
    source: Path,
    target: Path,
    *,
    bitrate: str = ASR_BITRATE,
    sample_rate: int = ASR_SAMPLE_RATE,
) -> None:
    """Convert ``source`` into the mono/low-rate form the ASR is fed."""
    target.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [
            'ffmpeg',
            '-y',
            '-v',
            'error',
            '-i',
            str(source),
            '-vn',
            '-ac',
            '1',
            '-ar',
            str(sample_rate),
            '-c:a',
            'libmp3lame',
            '-b:a',
            bitrate,
            str(target),
        ]
    )


def archive(source: Path, target: Path, *, bitrate: str) -> None:
    """Keep a listenable copy for the reader.

    ``original`` stores the downloaded file untouched — the point of archiving is
    that the episode can still be found later, so nothing is thrown away by
    default beyond a deliberate re-encode.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if bitrate == 'original':
        shutil.copyfile(source, target)
        return
    _run_ffmpeg(
        [
            'ffmpeg',
            '-y',
            '-v',
            'error',
            '-i',
            str(source),
            '-vn',
            '-c:a',
            'libmp3lame',
            '-b:a',
            bitrate,
            str(target),
        ]
    )


def _concat(parts: Sequence[Path], target: Path) -> Path:
    """Join consecutive segments without re-encoding.

    The concat demuxer takes a listing file; our paths come from a temporary
    directory hippo created, so no quoting edge case can reach it.
    """
    listing = target.with_name(f'{target.stem}.txt')
    listing.write_text(''.join(f"file '{part.resolve()}'\n" for part in parts), encoding='utf-8')
    try:
        _run_ffmpeg(
            [
                'ffmpeg',
                '-y',
                '-v',
                'error',
                '-f',
                'concat',
                '-safe',
                '0',
                '-i',
                str(listing),
                '-c',
                'copy',
                str(target),
            ]
        )
    finally:
        listing.unlink(missing_ok=True)
    return target


def split(
    audio: Path,
    workdir: Path,
    *,
    chunk_seconds: int,
    min_tail_seconds: int,
    max_chunk_seconds: int,
) -> list[Chunk]:
    """Cut ``audio`` into fixed-length chunks, folding a short tail into the last one.

    Offsets are accumulated from the probed duration of each part, so a chunk's
    ``start_seconds`` is exact rather than nominal.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [
            'ffmpeg',
            '-y',
            '-v',
            'error',
            '-i',
            str(audio),
            '-c',
            'copy',
            '-f',
            'segment',
            '-segment_time',
            str(chunk_seconds),
            '-reset_timestamps',
            '1',
            str(workdir / _PART_PATTERN),
        ]
    )
    parts = sorted(workdir.glob(_PART_PATTERN.replace('%03d', '*')))
    if not parts:
        raise AudioError('ffmpeg 没有生成任何音频切片')
    if len(parts) > 1 and probe_duration(parts[-1]) < min_tail_seconds:
        parts = [*parts[:-2], _concat(parts[-2:], workdir / 'tail.mp3')]

    chunks: list[Chunk] = []
    start = 0.0
    for index, part in enumerate(parts):
        duration = probe_duration(part)
        if duration > max_chunk_seconds:
            raise AudioError(
                f'切片 {index} 时长 {duration:.1f}s 超过上限 {max_chunk_seconds}s'
                '（chunk_seconds 与 min_tail_seconds 的组合会把切片撑大）'
            )
        chunks.append(
            Chunk(
                index=index,
                path=part,
                start_seconds=round(start, 3),
                duration_seconds=round(duration, 3),
            )
        )
        start += duration
    return chunks


def prepare(
    source: Path,
    workdir: Path,
    *,
    chunk_seconds: int,
    min_tail_seconds: int,
    max_chunk_seconds: int,
    bitrate: str = ASR_BITRATE,
) -> list[Chunk]:
    """Transcode ``source`` and cut it into the chunks we upload.

    Blocking by design: the caller runs it through ``asyncio.to_thread`` so the
    event loop keeps serving requests while ffmpeg works.
    """
    audio = workdir / AUDIO_NAME
    transcode(source, audio, bitrate=bitrate)
    return split(
        audio,
        workdir,
        chunk_seconds=chunk_seconds,
        min_tail_seconds=min_tail_seconds,
        max_chunk_seconds=max_chunk_seconds,
    )


__all__ = [
    'ASR_BITRATE',
    'ASR_SAMPLE_RATE',
    'AUDIO_NAME',
    'AudioError',
    'Chunk',
    'archive',
    'prepare',
    'probe_duration',
    'split',
    'transcode',
]
