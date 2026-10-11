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

from ..s3 import get_s3_client, guess_extension, upload_object_bytes, with_prefix

MEDIA_PREFIX = 'media'
DEFAULT_CONTENT_TYPE = 'audio/mpeg'

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

    A missing object store is an error the caller reports; it must not stop the
    transcription it was made for.
    """
    bundle = get_s3_client()
    if not bundle:
        raise ArchiveError('未配置对象存储（HIPPO_S3_*），无法留档音频')
    config, client = bundle
    scoped = with_prefix(config, prefix)
    payload = path.read_bytes()
    key = build_media_key(scoped.prefix, article_pk, kind, content_type)
    upload_object_bytes(client, bucket=scoped.bucket, key=key, payload=payload, content_type=content_type)
    return key, len(payload)


#: `bytes=0-1023` / `bytes=500-`. A suffix range (`bytes=-500`) is not worth
#: supporting for audio seeking and is answered with the whole file.
_RANGE_RE = re.compile(r'bytes=(\d*)-(\d*)')


@dataclass(frozen=True, slots=True)
class AudioStream:
    body: Any
    content_type: str
    size: int
    total: int


def parse_range(header: str | None, total: int | None) -> tuple[int | None, int | None]:
    """Read a Range header into inclusive bounds, or ``(None, None)`` for all of it."""
    if not header:
        return None, None
    matched = _RANGE_RE.fullmatch(header.strip())
    if not matched:
        return None, None
    raw_start, raw_end = matched.groups()
    if not raw_start:
        return None, None
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


def fetch_object(key: str, target: Path) -> int:
    """Copy an archived object back to disk. Used when a job's source is already stored.

    The size is verified against what S3 reports, because a media file that arrives
    half-written is worse than one that fails loudly.
    """
    bundle = get_s3_client()
    if not bundle:
        raise ArchiveError('未配置对象存储（HIPPO_S3_*），无法读取留档媒体')
    config, client = bundle
    try:
        response = client.get_object(Bucket=config.bucket, Key=key)
        expected = int(response.get('ContentLength') or 0)
        written = 0
        with target.open('wb') as handle:
            for chunk in response['Body'].iter_chunks(1 << 16):
                written += len(chunk)
                handle.write(chunk)
    except Exception as exc:
        raise ArchiveError(f'读取留档媒体失败：{exc}') from exc
    if not written or (expected and written != expected):
        raise ArchiveError(f'留档媒体不完整：{written}/{expected} 字节')
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
    'ArchiveError',
    'AudioStream',
    'build_media_key',
    'fetch_object',
    'open_audio',
    'parse_range',
    'store_media',
]
