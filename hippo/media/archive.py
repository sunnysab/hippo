"""Archiving the audio a transcript came from.

Keeping the audio is the point of the feature: a transcript is much harder to
check without its source, and "删了就再也找不回来了" is the failure this avoids.
The upload reuses the article-image S3 client, under its own prefix so the two
never collide.
"""

from __future__ import annotations

from pathlib import Path

from ..s3 import get_s3_client, guess_extension, upload_object_bytes, with_prefix

MEDIA_PREFIX = 'media'
DEFAULT_CONTENT_TYPE = 'audio/mpeg'


class ArchiveError(RuntimeError):
    pass


def build_media_key(prefix: str, article_pk: int, kind: str, content_type: str | None) -> str:
    return f'{prefix}{kind}/{article_pk}.{guess_extension(content_type)}'


def store_audio(
    *,
    article_pk: int,
    path: Path,
    content_type: str = DEFAULT_CONTENT_TYPE,
    kind: str = 'audio',
    prefix: str = MEDIA_PREFIX,
) -> tuple[str, int]:
    """Upload the audited audio file and return ``(key, size)``.

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


__all__ = ['DEFAULT_CONTENT_TYPE', 'MEDIA_PREFIX', 'ArchiveError', 'build_media_key', 'store_audio']
