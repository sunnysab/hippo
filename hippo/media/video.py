"""Muxing a downloaded video track and its audio into one watchable MP4.

The reader watches what Hippo keeps, so the archived video has to be a single MP4
with both tracks. A source hands back one of two things: a progressive MP4 that
already carries its audio (``ResolvedMedia.video_has_audio``), or a video-only
track whose sound travels in ``ResolvedMedia.audio_url`` and has to be muxed in.

Only the container is written here: both streams are copied, never re-encoded, so
merging costs a disk copy rather than an encode. The contract asks the sources for
H.264 picture and AAC sound, so no codec conversion and no fallback encoder live
here. ffmpeg runs over local files with ``-nostdin``, no shell, a wall clock and a
hard file size limit, and with the protocol whitelist applied to *each* input so a
source value that is not a plain file path can never make it open a socket.

This is not an OS sandbox. It removes the shell, the network and the size hazards
from a path handed to ffmpeg, and nothing about the rest of the process.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

#: A merge is a container copy, so anything short of a feature film finishes in
#: seconds; the ceiling exists so a stuck process cannot hold a worker forever.
MERGE_TIMEOUT_SECONDS = 600.0

#: Only protocols that read local files: no http, no manifest demuxer.
_LOCAL_PROTOCOLS = 'file,crypto,data'

FFMPEG_BIN = 'ffmpeg'
#: ``prlimit --fsize`` is what makes the size ceiling hard. Linux kills the writer
#: the moment it would pass the limit, so a truncated output fails loudly instead of
#: finishing politely with a smaller file — which is exactly what ``-fs`` does, and
#: why ``-fs`` is not used.
PRLIMIT_BIN = 'prlimit'


class VideoError(RuntimeError):
    pass


def _binary(name: str) -> str:
    """Absolute path of a required tool, or a fixed error naming it.

    A missing tool is an error, never a quiet fallback to a weaker merge: the size
    ceiling and the whitelist only hold while both binaries are actually there.
    """
    found = shutil.which(name)
    if not found:
        raise VideoError(f'{name} is not installed')
    return found


def _run(args: list[str], *, timeout: float) -> int:
    """Run the merge and keep nothing the process prints.

    stdout/stderr go to DEVNULL rather than a pipe: ffmpeg is chatty about the files
    it opens, and a pipe nobody reads is both an unbounded buffer and a way for a
    private path to end up in an error message. What a failure needs is the exit
    status, which the caller already reports.
    """
    try:
        completed = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoError(f'ffmpeg timed out after {timeout:.0f}s') from exc
    except OSError as exc:
        raise VideoError('ffmpeg could not be started') from exc
    return completed.returncode


def _ffmpeg_args(video: Path, audio: Path, target: Path) -> list[str]:
    """The merge command line, with the whitelist repeated for both inputs.

    ``-protocol_whitelist`` is an input option, so one copy protects only the ``-i``
    right after it; each file gets its own.
    """
    return [
        '-y',
        '-nostdin',
        '-v',
        'error',
        '-protocol_whitelist',
        _LOCAL_PROTOCOLS,
        '-i',
        str(video),
        '-protocol_whitelist',
        _LOCAL_PROTOCOLS,
        '-i',
        str(audio),
        '-map',
        '0:v:0',
        '-map',
        '1:a:0',
        '-c:v',
        'copy',
        '-c:a',
        'copy',
        # moov in front, so a reader can seek before the file has fully arrived.
        '-movflags',
        '+faststart',
        str(target),
    ]


def merge(
    video: Path,
    audio: Path,
    target: Path,
    *,
    max_bytes: int,
    timeout_seconds: float = MERGE_TIMEOUT_SECONDS,
) -> int:
    """Write ``video``'s picture and ``audio``'s sound into ``target``.

    Returns the size of the merged file. Raises ``VideoError`` when nothing usable
    came out. A failed, timed-out or oversized run never leaves its partial output
    behind: a short file in the object store is worse than a failure the queue can
    retry.
    """
    limit = int(max_bytes)
    if limit <= 0:
        raise VideoError('merge size ceiling must be positive')
    video = Path(video).resolve()
    audio = Path(audio).resolve()
    target = Path(target).resolve()
    # The unlink below plus `-y` would happily destroy an input if the caller ever
    # passed one as the target. Resolving both sides first catches a relative path
    # or a symlink alias, instead of truncating the file we were told to read.
    if target in (video, audio):
        raise VideoError('merge target must not be one of its inputs')
    ffmpeg = _binary(FFMPEG_BIN)
    prlimit = _binary(PRLIMIT_BIN)
    # A leftover from an earlier attempt must never be taken for this run's output.
    target.unlink(missing_ok=True)
    try:
        returncode = _run(
            [
                prlimit,
                f'--fsize={limit}:{limit}',
                # No core dump from a killed writer: the test matrix and a hostile
                # input both end in SIGXFSZ here, and a dump is just disk.
                '--core=0:0',
                '--',
                ffmpeg,
                *_ffmpeg_args(video, audio, target),
            ],
            timeout=timeout_seconds,
        )
        size = target.stat().st_size if target.is_file() else 0
        if returncode != 0:
            raise VideoError(f'ffmpeg failed (exit {returncode})')
        if not size:
            raise VideoError('ffmpeg produced an empty file')
        if size >= limit:
            raise VideoError(f'merge output reached the {limit} byte ceiling')
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return size


__all__ = ['MERGE_TIMEOUT_SECONDS', 'VideoError', 'merge']
