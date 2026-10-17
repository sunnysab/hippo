"""Storing a finished private upload in the knowledge-base object store.

This is the storage half of a knowledge-base upload and nothing else. The caller
owns the HTTP stream, the temporary file, the database row, the ownership check
and the per-attempt token, then hands the *finished* private file here. The file
is hashed and uploaded under a key built only from internal identifiers, so no
caller file name or local path can reach the bucket, and the document is never
held in memory whole.

Storage is not authorisation: :func:`store_document` cannot tell whether the
caller was allowed to upload, and :func:`delete_document_object` is an internal
helper for code that already passed its own owner/lease check. Neither is a DB
or HTTP interface. :func:`download_document` is the read side of the same
private path: it streams one verified object back into a fresh local file for
the parser, and it is not an HTTP interface either.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from boto3.s3.transfer import TransferConfig

from ..s3 import get_s3_client, with_prefix
from .parse import CODE_TOO_LARGE, CODE_UNSUPPORTED, MAX_FILE_BYTES

#: Fixed knowledge-base prefix. Distinct from the article-image (``mp/image/``)
#: and archived-media (``media/``) prefixes, so a knowledge key never collides
#: with either.
KNOWLEDGE_PREFIX = 'knowledge'

#: Only these canonical types are accepted. The extension is the only thing the
#: key takes from the declared type; the caller's file name is never used.
_MIME_EXTENSIONS = {'application/pdf': 'pdf', 'text/plain': 'txt'}

#: Characters a canonical lower-case SHA-256 digest may contain.
_HEX_CHARS = frozenset('0123456789abcdef')

#: One read block for hashing: the file is streamed, never read whole.
_READ_CHUNK_BYTES = 64 * 1024

#: One part at a time, with the classic transfer client, so the SDK does not
#: parallelise parts (and hold several in memory) behind our back.
_UPLOAD_TRANSFER = TransferConfig(max_concurrency=1, preferred_transfer_client='classic')

#: One monotonic budget for one download, checked at our own get/read/write
#: boundaries. A single blocking SDK call cannot be interrupted from Python, so
#: this is not a hard wall-clock limit.
_DOWNLOAD_BUDGET_SECONDS = 120.0

CODE_INVALID_INPUT = 'document.invalid_input'
CODE_EMPTY = 'document.empty'
CODE_UNREADABLE = 'document.unreadable'
CODE_CHANGED = 'document.changed'
CODE_NOT_CONFIGURED = 'document.store_unconfigured'
CODE_STORE_UNAVAILABLE = 'document.store_unavailable'
CODE_UPLOAD_FAILED = 'document.upload_failed'
CODE_DELETE_FAILED = 'document.delete_failed'
CODE_DOWNLOAD_FAILED = 'document.download_failed'
CODE_CONTENT_MISMATCH = 'document.content_mismatch'
CODE_DOWNLOAD_TIMEOUT = 'document.download_timeout'

#: Two fixed sentences for the read side, long enough to exceed a line on their
#: own; they carry no SDK text, key, bucket or path.
_MSG_DOWNLOAD_FAILED = 'The document could not be downloaded.'
_MSG_MISMATCH = 'The downloaded document does not match its record.'


class DocumentStoreError(RuntimeError):
    """A storage failure carrying a stable ASCII ``code`` and a path-free message.

    ``str(error)`` is a generic English sentence: object-store text, credentials
    and private local paths stay on the ``__cause__`` chain for logs, never in
    the caller-facing message. ``cleanup_failed`` says whether this attempt's
    object could not be removed; it is internal information, not part of any
    public response.
    """

    def __init__(self, code: str, message: str, *, cleanup_failed: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.cleanup_failed = cleanup_failed


@dataclass(frozen=True, slots=True)
class StoredDocument:
    """The internal result of one upload: object key, real size, real SHA-256.

    The key names a private object. It belongs in the caller's database row; an
    HTTP layer must never serialize it (or the SHA-256) to the client.
    """

    key: str
    size_bytes: int
    sha256: str


def build_document_key(
    *,
    knowledge_base_id: str,
    document_id: str,
    upload_token: str,
    mime_type: str,
) -> str:
    """The object key for one upload attempt, without touching any SDK or file.

    Pure by design: the upload state machine persists this attempt's key before
    the object store is contacted, so a crash can still clean up the orphan.
    The identifiers must be canonical UUIDs and the type one of the two
    canonical ones; the key takes nothing from a caller file name or path.
    """
    mime = _canonical_mime(mime_type)
    if mime is None:
        raise DocumentStoreError(CODE_UNSUPPORTED, 'This file type is not supported.')
    return (
        f'{KNOWLEDGE_PREFIX}/'
        f'{_canonical_uuid(knowledge_base_id)}/'
        f'{_canonical_uuid(document_id)}/'
        f'{_canonical_uuid(upload_token)}.{_MIME_EXTENSIONS[mime]}'
    )


def store_document(
    *,
    path: Path,
    knowledge_base_id: str,
    document_id: str,
    upload_token: str,
    mime_type: str,
) -> StoredDocument:
    """Store one finished private file and return its key, size and SHA-256.

    All three identifiers must be canonical UUIDs; the type must be exactly
    ``application/pdf`` or ``text/plain``. The real SHA-256 and length are
    computed from the bytes in 64 KiB blocks, so no caller-declared value is
    trusted and no whole-file read happens.

    A file that changes between the pre-upload stat and the post-upload stat is
    an error, and this attempt's object is removed. Any other failure removes
    only this attempt's key: another attempt's or an earlier winner's object is
    never touched. Parameter and size pre-checks run first and never reach the
    object store.
    """
    mime = _canonical_mime(mime_type)
    if mime is None:
        raise DocumentStoreError(CODE_UNSUPPORTED, 'This file type is not supported.')
    key = build_document_key(
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
        upload_token=upload_token,
        mime_type=mime,
    )
    source = _source_path(path)
    before = _regular_file(source)
    if before.st_size == 0:
        raise DocumentStoreError(CODE_EMPTY, 'The document is empty.')
    if before.st_size > MAX_FILE_BYTES:
        raise DocumentStoreError(CODE_TOO_LARGE, 'The document exceeds the supported size limits.')

    bundle = _s3_client()
    if not bundle:
        raise DocumentStoreError(CODE_NOT_CONFIGURED, 'Object storage is not configured.')
    config, client = bundle
    scoped = with_prefix(config, KNOWLEDGE_PREFIX)

    size, sha256 = _hash_file(source)
    if size != before.st_size:
        # The private file changed while it was being hashed; the upload has not
        # started, so there is no attempt object to clean up.
        raise DocumentStoreError(CODE_CHANGED, 'The document changed while it was being prepared.')

    try:
        client.upload_file(
            Filename=str(source),
            Bucket=scoped.bucket,
            Key=key,
            ExtraArgs={'ContentType': mime},
            Config=_UPLOAD_TRANSFER,
        )
    except Exception as exc:
        cleaned = _delete_attempt(client, scoped.bucket, key)
        raise DocumentStoreError(
            CODE_UPLOAD_FAILED,
            'The document could not be stored.',
            cleanup_failed=not cleaned,
        ) from exc

    after = _stat_or_none(source)
    # shortcut: a same-size, same-mtime swap passes; a private completed file only
    # needs guarding against accidental change, not a hostile replacement.
    if after is None or _fingerprint(after) != _fingerprint(before):
        cleaned = _delete_attempt(client, scoped.bucket, key)
        raise DocumentStoreError(
            CODE_CHANGED,
            'The document changed while it was being uploaded.',
            cleanup_failed=not cleaned,
        )
    return StoredDocument(key=key, size_bytes=size, sha256=sha256)


def delete_document_object(key: str) -> None:
    """Delete exactly one known knowledge object key.

    Internal only: ``key`` comes from our own row, never from an HTTP client.
    Only that single key is deleted — no listing, no prefix or batch delete, and
    nothing inferred. Under the default S3 delete semantics this is idempotent
    for a missing object; a real failure raises instead of pretending success.
    """
    _knowledge_key(key)
    bundle = _s3_client()
    if not bundle:
        raise DocumentStoreError(CODE_NOT_CONFIGURED, 'Object storage is not configured.')
    config, client = bundle
    try:
        client.delete_object(Bucket=config.bucket, Key=key)
    except Exception as exc:
        raise DocumentStoreError(CODE_DELETE_FAILED, 'The stored document could not be deleted.') from exc


def download_document(
    *,
    key: str,
    path: Path,
    size_bytes: int,
    sha256: str,
) -> None:
    """Stream one known private object into a *new* local file.

    Internal only: ``key`` comes from our own verified row and the caller passes
    a fixed fresh path inside its own temporary directory, so the key is never
    turned into a local path. The expected size and SHA-256 are the values the
    caller already recorded; the real byte count and digest are computed from
    the stream and must match exactly, so a short, long or altered stream fails
    instead of leaving a partial file behind. When the SDK reports
    ``ContentLength`` it must match the expected size; a missing one is fine,
    because only the bytes actually read decide success.

    The target must not exist: it is created exclusively, so an existing file or
    a symlink is refused and never overwritten or removed. A failure removes
    only the fresh target this call created — the source object is never
    deleted, listed or rewritten and no database row is touched. The fixed
    monotonic budget is checked at our own get/read/write boundaries and once
    more after the final EOF read, the body close and the file flush/close, so a
    stream that only crosses the budget at the very end fails instead of being
    reported as a success; a single blocking SDK call cannot be interrupted from
    Python, so this is not a hard wall-clock limit either.
    """
    expected_size = _expected_size(size_bytes)
    expected_sha256 = _expected_sha256(sha256)
    _knowledge_key(key)
    target = _source_path(path)

    bundle = _s3_client()
    if not bundle:
        raise DocumentStoreError(CODE_NOT_CONFIGURED, 'Object storage is not configured.')
    config, client = bundle

    try:
        handle = target.open('xb')
    except FileExistsError as exc:
        raise DocumentStoreError(
            CODE_INVALID_INPUT,
            'The download target already exists.',
        ) from exc
    except OSError as exc:
        raise DocumentStoreError(CODE_DOWNLOAD_FAILED, _MSG_DOWNLOAD_FAILED) from exc

    deadline = time.monotonic() + _DOWNLOAD_BUDGET_SECONDS
    try:
        _stream_object(
            client=client,
            bucket=config.bucket,
            key=key,
            handle=handle,
            size_bytes=expected_size,
            sha256=expected_sha256,
            deadline=deadline,
        )
        _finish_target(handle)
        # The final EOF read, body close and file flush/close are not boundary
        # checks of their own, so the budget is checked once more after them.
        _check_deadline(deadline)
    except BaseException:
        _discard_target(handle, target)
        raise


# -- helpers -----------------------------------------------------------------


def _s3_client() -> tuple[Any, Any] | None:
    """The configured client bundle, or ``None`` when storage is unconfigured.

    Building the client can fail (endpoint, credential plumbing); that failure is
    wrapped so its type, text and private configuration stay on the cause.
    """
    try:
        return get_s3_client()
    except Exception as exc:
        raise DocumentStoreError(
            CODE_STORE_UNAVAILABLE,
            'Object storage is unavailable.',
        ) from exc


def _canonical_mime(mime_type: Any) -> str | None:
    """The declared type, or ``None`` unless it is one of the two canonical ones."""
    if not isinstance(mime_type, str) or mime_type not in _MIME_EXTENSIONS:
        return None
    return mime_type


def _canonical_uuid(value: Any) -> str:
    """The canonical lower-case form, or ``DocumentStoreError`` for anything else.

    Braces/URN/upper-case input parses as a UUID but is not the canonical form a
    caller is expected to hold, so it is rejected rather than silently rewritten.
    """
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise DocumentStoreError(CODE_INVALID_INPUT, 'An identifier is not a canonical UUID.') from exc
    if not isinstance(value, str) or str(parsed) != value:
        raise DocumentStoreError(CODE_INVALID_INPUT, 'An identifier is not a canonical UUID.')
    return str(parsed)


def _source_path(path: Path) -> Path:
    try:
        return Path(os.fspath(path))
    except (TypeError, ValueError) as exc:
        raise DocumentStoreError(CODE_INVALID_INPUT, 'The document path is invalid.') from exc


def _regular_file(path: Path) -> os.stat_result:
    try:
        info = path.stat()
    except OSError as exc:
        raise DocumentStoreError(CODE_UNREADABLE, 'The document could not be read.') from exc
    if not stat.S_ISREG(info.st_mode):
        raise DocumentStoreError(CODE_UNREADABLE, 'The document is not a regular file.')
    return info


def _stat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except OSError:
        return None


def _fingerprint(info: os.stat_result) -> tuple[int, int, int, int]:
    """Identity, length and mtime of the private file; any change invalidates it."""
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _hash_file(path: Path) -> tuple[int, str]:
    """Real byte count and SHA-256, read in fixed blocks and never held whole."""
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open('rb') as handle:
            while True:
                chunk = handle.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise DocumentStoreError(
                        CODE_TOO_LARGE,
                        'The document exceeds the supported size limits.',
                    )
                digest.update(chunk)
    except DocumentStoreError:
        raise
    except OSError as exc:
        raise DocumentStoreError(CODE_UNREADABLE, 'The document could not be read.') from exc
    return total, digest.hexdigest()


def _delete_attempt(client: Any, bucket: str, key: str) -> bool:
    """Remove this attempt's object; ``False`` when it could not be removed."""
    try:
        client.delete_object(Bucket=bucket, Key=key)
    except Exception:
        return False
    return True


def _knowledge_key(key: Any) -> str:
    """A key under the knowledge prefix, or the stable error both helpers use."""
    if not isinstance(key, str) or not key.startswith(f'{KNOWLEDGE_PREFIX}/'):
        raise DocumentStoreError(CODE_INVALID_INPUT, 'The object key is not a knowledge document key.')
    return key


def _expected_size(value: Any) -> int:
    """A plain byte count in ``1..MAX_FILE_BYTES``, or the generic input error."""
    valid = not isinstance(value, bool) and isinstance(value, int) and 1 <= value <= MAX_FILE_BYTES
    if not valid:
        raise DocumentStoreError(CODE_INVALID_INPUT, 'The document size is not valid.')
    return value


def _expected_sha256(value: Any) -> str:
    """A 64-character lower-case hex digest, or the generic input error."""
    if not isinstance(value, str) or len(value) != 64 or not set(value) <= _HEX_CHARS:
        raise DocumentStoreError(CODE_INVALID_INPUT, 'The document checksum is not valid.')
    return value


def _stream_object(
    *,
    client: Any,
    bucket: str,
    key: str,
    handle: Any,
    size_bytes: int,
    sha256: str,
    deadline: float,
) -> None:
    """Read the object in bounded blocks, hashing and writing as it goes.

    Bytes past the expected size are refused before the excess is written, and
    the real count and digest are compared with the expected ones only once the
    stream reached EOF.
    """
    body = _open_body(
        client=client,
        bucket=bucket,
        key=key,
        size_bytes=size_bytes,
        deadline=deadline,
    )
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            block = _read_block(body, deadline)
            if not block:
                break
            total += len(block)
            if total > size_bytes:
                raise DocumentStoreError(CODE_CONTENT_MISMATCH, _MSG_MISMATCH)
            digest.update(block)
            _write_block(handle, block, deadline)
        _close_body(body)
    except BaseException:
        _close_body_quietly(body)
        raise
    if total != size_bytes or digest.hexdigest() != sha256:
        raise DocumentStoreError(CODE_CONTENT_MISMATCH, _MSG_MISMATCH)


def _open_body(*, client: Any, bucket: str, key: str, size_bytes: int, deadline: float) -> Any:
    """Fetch the object body, refusing a present ``ContentLength`` that disagrees."""
    _check_deadline(deadline)
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        raise DocumentStoreError(CODE_DOWNLOAD_FAILED, _MSG_DOWNLOAD_FAILED) from exc
    body = response.get('Body')
    if body is None:
        raise DocumentStoreError(CODE_DOWNLOAD_FAILED, _MSG_DOWNLOAD_FAILED)
    if 'ContentLength' in response:
        declared = response['ContentLength']
        if isinstance(declared, bool) or not isinstance(declared, int) or declared != size_bytes:
            _close_body_quietly(body)
            raise DocumentStoreError(CODE_CONTENT_MISMATCH, _MSG_MISMATCH)
    return body


def _read_block(body: Any, deadline: float) -> bytes:
    """One explicitly bounded read; the body is never asked for everything."""
    _check_deadline(deadline)
    try:
        block = body.read(_READ_CHUNK_BYTES)
    except Exception as exc:
        raise DocumentStoreError(CODE_DOWNLOAD_FAILED, _MSG_DOWNLOAD_FAILED) from exc
    if not block:
        return b''
    if not isinstance(block, bytes):
        raise DocumentStoreError(CODE_DOWNLOAD_FAILED, _MSG_DOWNLOAD_FAILED)
    return block


def _write_block(handle: Any, block: bytes, deadline: float) -> None:
    """Write one block fully; a short write is a failed download, not a retry."""
    _check_deadline(deadline)
    try:
        written = handle.write(block)
    except Exception as exc:
        raise DocumentStoreError(CODE_DOWNLOAD_FAILED, _MSG_DOWNLOAD_FAILED) from exc
    if written != len(block):
        raise DocumentStoreError(CODE_DOWNLOAD_FAILED, _MSG_DOWNLOAD_FAILED)


def _close_body(body: Any) -> None:
    """Close the SDK body; a close failure fails the download too."""
    try:
        body.close()
    except Exception as exc:
        raise DocumentStoreError(CODE_DOWNLOAD_FAILED, _MSG_DOWNLOAD_FAILED) from exc


def _close_body_quietly(body: Any) -> None:
    """Best-effort close on a failure path, which must not mask the real error."""
    with contextlib.suppress(Exception):
        body.close()


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise DocumentStoreError(CODE_DOWNLOAD_TIMEOUT, 'The document download took too long.')


def _finish_target(handle: Any) -> None:
    """Flush and close the fresh file; either failing means no usable download."""
    try:
        handle.flush()
        handle.close()
    except Exception as exc:
        raise DocumentStoreError(CODE_DOWNLOAD_FAILED, _MSG_DOWNLOAD_FAILED) from exc


def _discard_target(handle: Any, target: Path) -> None:
    """Remove only the fresh target this call created, without masking the error.

    Reached only after the exclusive create, so the path is this call's own
    file; a cleanup failure leaves it behind instead of pretending the download
    succeeded.
    """
    with contextlib.suppress(Exception):
        handle.close()
    with contextlib.suppress(OSError):
        target.unlink()


__all__ = [
    'KNOWLEDGE_PREFIX',
    'DocumentStoreError',
    'StoredDocument',
    'build_document_key',
    'delete_document_object',
    'download_document',
    'store_document',
]
