"""Archiving the audio a transcript came from.

Keeping the audio is the point of the feature: a transcript is much harder to
check without its source, and "删了就再也找不回来了" is the failure this avoids.
The upload reuses the article-image S3 client, under its own prefix so the two
never collide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from boto3.s3.transfer import TransferConfig

from ..logger import get_logger
from ..s3 import get_s3_client, guess_extension, with_prefix

logger = get_logger(__name__)

MEDIA_PREFIX = 'media'
DEFAULT_CONTENT_TYPE = 'audio/mpeg'

#: A fixed, small concurrency, with the classic transfer client: media files are
#: big, so the parallel-part transfer must not be picked (and hold several parts in
#: memory) behind our back.
_UPLOAD_CONCURRENCY = 1
_UPLOAD_TRANSFER = TransferConfig(max_concurrency=_UPLOAD_CONCURRENCY, preferred_transfer_client='classic')

#: The archive kind of the video file itself, as opposed to its audio.
VIDEO_KIND = 'video'

#: What ``article_media.kind`` may hold. `source` is the untouched download, `video`
#: is the video itself (a file somebody already had), `audio` is the listenable copy.
MEDIA_KINDS = ('audio', 'video', 'source')

#: Content type per kind of object we store, used when the caller does not know better.
CONTENT_TYPES = {
    'audio': 'audio/mpeg',
    'video': 'video/mp4',
    'source': 'application/octet-stream',
}


class ArchiveError(RuntimeError):
    pass


def build_media_key(prefix: str, article_pk: int, kind: str, content_type: str | None) -> str:
    return f'{prefix}{kind}/{article_pk}.{guess_extension(content_type)}'


def store_media(
    *,
    article_pk: int,
    path: Path,
    content_type: str = DEFAULT_CONTENT_TYPE,
    kind: str = 'audio',
    prefix: str = MEDIA_PREFIX,
) -> tuple[str, int]:
    """Upload one article's media file and return ``(key, size)``.

    Audio and video are the same operation with a different key and content type, so
    there is one function: the object store does not care which it is holding.

    The file is streamed from disk by the object store client; it is never held in
    memory whole, because it can be an hour of audio. A file that changed while it
    was being uploaded is an error rather than a success whose size no longer
    describes the stored object.

    A missing object store is an error the caller reports; it must not stop the
    transcription it was made for.
    """
    bundle = get_s3_client()
    if not bundle:
        raise ArchiveError('未配置对象存储（HIPPO_S3_*），无法留档音频')
    config, client = bundle
    scoped = with_prefix(config, prefix)
    before = path.stat()
    size = before.st_size
    key = build_media_key(scoped.prefix, article_pk, kind, content_type)
    extra_args: dict[str, Any] = {'ContentType': content_type} if content_type else {}
    client.upload_file(
        Filename=str(path),
        Bucket=scoped.bucket,
        Key=key,
        ExtraArgs=extra_args,
        Config=_UPLOAD_TRANSFER,
    )
    after = path.stat()
    # shortcut: a same-size, same-metadata swap passes; a private completed file only
    # needs guarding against accidental change, not against a hostile replacement.
    if (
        after.st_size != size
        or after.st_mtime_ns != before.st_mtime_ns
        or (after.st_ino, after.st_dev) != (before.st_ino, before.st_dev)
    ):
        raise ArchiveError('Media file changed during upload')
    return key, size


#: Single byte ranges, including suffix ranges when the full length is known.
_RANGE_RE = re.compile(r'bytes\s*=\s*(\d*)\s*-\s*(\d*)', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class AudioStream:
    body: Any
    content_type: str
    size: int
    total: int


def parse_range(header: str | None, total: int | None) -> tuple[int | None, int | None]:
    """Read a Range header into inclusive bounds, or ``(None, None)`` for all of it.

    ``bytes=-N`` is the last N bytes; without a known ``total`` that length cannot
    be computed, so the whole file is served instead of a claimed suffix. A range
    that starts at or past the end, and an empty suffix (``-0``, or any suffix of
    an empty object), have no representation and raise ``ArchiveError`` — the
    caller answers 416 with the real length.
    """
    if not header:
        return None, None
    matched = _RANGE_RE.fullmatch(header.strip())
    if not matched:
        return None, None
    raw_start, raw_end = matched.groups()
    if not raw_start:
        if not raw_end or total is None:
            # The whole file without a known total, or a suffix of nothing.
            return None, None
        suffix_length = int(raw_end)
        if suffix_length == 0 or total == 0:
            raise ArchiveError('请求的范围不合法')
        if suffix_length >= total:
            return 0, total - 1
        return total - suffix_length, total - 1
    start = int(raw_start)
    end = int(raw_end) if raw_end else None
    if total is not None:
        if start >= total:
            raise ArchiveError('请求的范围超出文件大小')
        if end is not None:
            end = min(end, total - 1)
    if end is not None and end < start:
        raise ArchiveError('请求的范围不合法')
    return start, end


def _close_body(response: Any) -> None:
    """Release the streaming body, swallowing a close failure.

    A body that cannot be closed is a connection the pool will deal with; it must not
    replace the reason the read failed, which is the only thing the caller can act on.
    """
    try:
        body = response.get('Body') if isinstance(response, dict) else None
        close = getattr(body, 'close', None)
        if close is not None:
            close()
    except Exception:
        logger.warning('failed to close the archived media stream')


def fetch_object(key: str, target: Path, *, max_bytes: int | None = None) -> int:
    """Copy an archived object back to disk. Used when a job's source is already stored.

    The size is verified against what S3 reports, because a media file that arrives
    half-written is worse than one that fails loudly.

    ``max_bytes`` bounds the copy the same way a download is bounded. The declared
    length is refused before a byte moves, and the running count is the real ceiling:
    the object store can always report a length that is not the one it then sends.
    A failure removes the partial file, so a retry does not resume onto garbage.
    """
    bundle = get_s3_client()
    if not bundle:
        raise ArchiveError('未配置对象存储（HIPPO_S3_*），无法读取留档媒体')
    config, client = bundle
    written = 0
    expected = 0
    response: Any = None
    target.unlink(missing_ok=True)
    try:
        response = client.get_object(Bucket=config.bucket, Key=key)
        expected = int(response.get('ContentLength') or 0)
        if max_bytes is not None and expected > max_bytes:
            raise ArchiveError(f'archived media declares {expected} bytes, over the {max_bytes} byte ceiling')
        with target.open('wb') as handle:
            for chunk in response['Body'].iter_chunks(1 << 16):
                written += len(chunk)
                if max_bytes is not None and written > max_bytes:
                    raise ArchiveError(f'archived media exceeds the {max_bytes} byte ceiling')
                handle.write(chunk)
        # 长度校验必须在清理路径里面：短读的 partial 不能留在磁盘上被下一次当源。
        if not written or (expected and written != expected):
            raise ArchiveError(f'archived media incomplete: {written}/{expected} bytes')
    except ArchiveError:
        target.unlink(missing_ok=True)
        raise
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise ArchiveError(f'failed to read archived media: {exc}') from exc
    finally:
        _close_body(response)
    return written


def open_audio(key: str, *, start: int | None = None, end: int | None = None) -> AudioStream:
    """Open an archived object for reading.

    ``key`` is used verbatim: it already carries the prefix, because that is what
    ``store_media`` saved. The bucket comes from the shared S3 config.
    """
    bundle = get_s3_client()
    if not bundle:
        raise ArchiveError('未配置对象存储（HIPPO_S3_*），无法读取留档音频')
    config, client = bundle
    kwargs: dict[str, Any] = {}
    if start is not None:
        kwargs['Range'] = f'bytes={start}-{end if end is not None else ""}'
    try:
        response = client.get_object(Bucket=config.bucket, Key=key, **kwargs)
    except Exception as exc:
        raise ArchiveError(f'读取留档音频失败：{exc}') from exc
    size = int(response.get('ContentLength') or 0)
    total = size
    content_range = str(response.get('ContentRange') or '')
    if '/' in content_range:
        _, _, raw_total = content_range.partition('/')
        if raw_total.isdigit():
            total = int(raw_total)
    return AudioStream(
        body=response['Body'],
        content_type=str(response.get('ContentType') or DEFAULT_CONTENT_TYPE),
        size=size,
        total=total,
    )


__all__ = [
    'CONTENT_TYPES',
    'DEFAULT_CONTENT_TYPE',
    'MEDIA_KINDS',
    'MEDIA_PREFIX',
    'VIDEO_KIND',
    'ArchiveError',
    'AudioStream',
    'build_media_key',
    'fetch_object',
    'open_audio',
    'parse_range',
    'store_media',
]
