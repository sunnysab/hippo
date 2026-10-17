"""Receiving one raw knowledge-base upload into the private object store.

This is the service half of a knowledge-base upload: it owns the document state
machine (``pending_upload`` -> ``uploading`` -> ``queued``) and the private
temporary file, and nothing else. The HTTP layer owns the request body and the
response whitelist, the import worker owns parsing. The returned row is a
private storage row and carries ``document.s3_key``/``sha256`` and the lease
columns, so an adapter must project it through its response serializer and never
return it as-is.

The order is deliberate: the owner and the state are checked, and the attempt's
token and key are journalled in a short committed transaction, before one body
byte is consumed - so a rejected request never touches the object store, and
every network phase runs without a database transaction held.

Not implemented here: an expiry reaper for abandoned ``uploading`` rows, the
import worker and the ready re-upload. A disconnected or crashed attempt leaves
its key journalled on the row for a later cleanup pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import tempfile
import uuid
from collections.abc import AsyncIterable, Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .parse import MAX_FILE_BYTES
from .store import (
    DocumentStoreError,
    StoredDocument,
    build_document_key,
    delete_document_object,
    store_document,
)

if TYPE_CHECKING:
    from ..storage import PostgresStorage

#: Whole-body wall clock for one request. A fixed internal limit, not a client
#: option; the HTTP layer is expected to reject oversized headers before this.
RECEIVE_TIMEOUT_SECONDS = 120

#: Private working directory and fixed file name: nothing from the request (its
#: file name, declared type or ids) reaches a local path.
_TEMP_DIRECTORY_PREFIX = 'hippo-knowledge-upload-'
_TEMP_FILE_NAME = 'document'

#: States in which the document already owns its object. That object is
#: published content, so this attempt must never delete it.
_PUBLISHED_STATES = ('queued', 'processing', 'ready')

# -- stable ASCII error codes ------------------------------------------------

CODE_NOT_FOUND = 'document.not_found'
CODE_CONFLICT = 'document.conflict'
CODE_TOO_LARGE = 'document.too_large'
CODE_EMPTY = 'document.empty'
CODE_UPLOAD_TIMEOUT = 'document.upload_timeout'
CODE_UPLOAD_FAILED = 'document.upload_failed'
CODE_PERSISTENCE_FAILED = 'document.persistence_failed'
CODE_DUPLICATE = 'document.duplicate'

# Fixed English sentences: storage text, SDK text, SQL text and private object
# keys stay on the ``__cause__`` chain for logs, never in a caller-facing code.
_MSG_NOT_FOUND = 'The document does not exist.'
_MSG_CONFLICT = 'The document is not waiting for an upload.'
_MSG_TOO_LARGE = 'The document exceeds the supported size limits.'
_MSG_EMPTY = 'The document is empty.'
_MSG_TIMEOUT = 'The upload timed out.'
_MSG_UPLOAD_FAILED = 'The document could not be uploaded.'
_MSG_PERSISTENCE_FAILED = 'The upload result could not be confirmed.'
_MSG_DUPLICATE = 'A document with the same content already exists in the knowledge base.'

#: A store result that does not describe this attempt. It is handled like any
#: other unknown transfer failure: never published, and only our own key removed.
_MSG_MISMATCH = 'The stored document does not match the received bytes.'


class DocumentUploadError(Exception):
    """An upload failure carrying a stable ASCII ``code`` and a generic message.

    ``str(error)`` is a fixed English sentence safe to map onto an HTTP status;
    object keys, SQL, SDK text and private local paths stay on the
    ``__cause__`` chain. ``code`` is one of the module-level ``document.*``
    constants.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class _Guarded:
    """The settled outcome of one cancellable unit of work.

    ``cancelled`` records that the surrounding request was cancelled while the
    unit ran. The unit still ran to completion, so ``result``/``error`` is its
    real outcome and the caller decides what to keep or clean before re-raising.
    """

    result: Any = None
    error: BaseException | None = None
    cancelled: bool = False


async def receive_document_file(
    *,
    storage: PostgresStorage,
    user_id: int,
    document_id: str,
    chunks: AsyncIterable[bytes],
) -> dict[str, Any]:
    """Receive one raw body for an owned ``pending_upload`` document.

    The attempt gets a fresh server-side ``uuid4`` token and a key built from the
    row's own canonical identifiers; no token, key, digest or file name is
    accepted from the caller. On success the row is ``queued`` and the returned
    dict is the internal ``document`` row - it contains ``s3_key``, ``sha256``
    and the lease columns, so an HTTP adapter must project it through the
    response whitelist instead of returning it whole.

    Raises :class:`DocumentUploadError` with a stable ASCII code for every
    failure. A failure that left the object possibly stored keeps the attempt's
    key journalled on the row for a later cleanup pass instead of pretending it
    is gone.
    """
    row = await _pending_document(storage, user_id, document_id)
    knowledge_base_id = str(row['knowledge_base_id'])
    canonical_id = str(row['id'])
    token = str(uuid.uuid4())
    key = _attempt_key(knowledge_base_id, canonical_id, token, row.get('mime_type'))
    await _begin_attempt(storage, user_id, canonical_id, token, key)
    return await _receive_and_publish(
        storage,
        user_id=user_id,
        document_id=canonical_id,
        knowledge_base_id=knowledge_base_id,
        token=token,
        key=key,
        mime_type=row['mime_type'],
        chunks=chunks,
    )


# -- phases ------------------------------------------------------------------


async def _pending_document(storage: PostgresStorage, user_id: int, document_id: str) -> dict[str, Any]:
    """The caller's live, free ``pending_upload`` row, or a ``DocumentUploadError``.

    Missing, malformed, foreign and tombstoned ids all answer ``not_found`` so
    the response never reveals whether the row exists. This runs before the
    body, the temporary directory and the object store are touched, and inside a
    short transaction so no read transaction is left open across the upload.
    """
    async with storage.transaction():
        row = await storage.knowledge.get_document(user_id, document_id)
    if row is None:
        raise DocumentUploadError(CODE_NOT_FOUND, _MSG_NOT_FOUND)
    if row.get('state') != 'pending_upload':
        raise DocumentUploadError(CODE_CONFLICT, _MSG_CONFLICT)
    return row


def _attempt_key(knowledge_base_id: str, document_id: str, token: str, mime_type: Any) -> str:
    """This attempt's pure object key; a row whose type is unusable fails closed."""
    try:
        return build_document_key(
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            upload_token=token,
            mime_type=mime_type,
        )
    except DocumentStoreError as exc:
        raise DocumentUploadError(CODE_UPLOAD_FAILED, _MSG_UPLOAD_FAILED) from exc


async def _begin_attempt(
    storage: PostgresStorage,
    user_id: int,
    document_id: str,
    token: str,
    key: str,
) -> None:
    """CAS the row to ``uploading`` and commit the token/key journal in one short transaction.

    The journal is durable before the object store is contacted, so a crash can
    still find the orphan. A lost CAS is re-checked: a row that is gone, foreign
    or deleted is ``not_found``; anything else is ``conflict``.
    """
    async with storage.transaction():
        began = await storage.knowledge.begin_upload(user_id, document_id, token, s3_key=key)
        if began:
            return
        current = await storage.knowledge.get_document(user_id, document_id)
    if current is None:
        raise DocumentUploadError(CODE_NOT_FOUND, _MSG_NOT_FOUND)
    raise DocumentUploadError(CODE_CONFLICT, _MSG_CONFLICT)


async def _receive_and_publish(
    storage: PostgresStorage,
    *,
    user_id: int,
    document_id: str,
    knowledge_base_id: str,
    token: str,
    key: str,
    mime_type: str,
    chunks: AsyncIterable[bytes],
) -> dict[str, Any]:
    """Receive the body into a private file, store it and publish the row."""
    with tempfile.TemporaryDirectory(prefix=_TEMP_DIRECTORY_PREFIX) as directory:
        path = Path(directory) / _TEMP_FILE_NAME
        size, sha256 = await _receive_body(
            storage,
            chunks,
            path,
            user_id=user_id,
            document_id=document_id,
            token=token,
        )
        await _store_attempt(
            storage,
            user_id=user_id,
            document_id=document_id,
            token=token,
            key=key,
            knowledge_base_id=knowledge_base_id,
            mime_type=mime_type,
            path=path,
            size=size,
            sha256=sha256,
        )
        return await _publish(
            storage,
            user_id=user_id,
            document_id=document_id,
            token=token,
            key=key,
            size=size,
            sha256=sha256,
        )


async def _receive_body(
    storage: PostgresStorage,
    chunks: AsyncIterable[bytes],
    path: Path,
    *,
    user_id: int,
    document_id: str,
    token: str,
) -> tuple[int, str]:
    """Stream the body to ``path`` and return its real size and SHA-256.

    Every failure here happened before any object could exist, so the journal is
    cleared with a trusted ``cleanup_succeeded=True`` and the private file is
    removed by the caller's temporary directory.
    """
    try:
        size, sha256 = await _drain_body(chunks, path)
    except asyncio.CancelledError:
        await _fail_quietly(storage, user_id, document_id, token, cleaned=True)
        raise
    except DocumentUploadError as exc:
        await _fail_attempt(
            storage,
            user_id,
            document_id,
            token,
            cleaned=True,
            code=exc.code,
            message=exc.message,
        )
        raise
    except TimeoutError as exc:
        await _fail_attempt(
            storage,
            user_id,
            document_id,
            token,
            cleaned=True,
            code=CODE_UPLOAD_TIMEOUT,
            message=_MSG_TIMEOUT,
        )
        raise DocumentUploadError(CODE_UPLOAD_TIMEOUT, _MSG_TIMEOUT) from exc
    except Exception as exc:
        await _fail_attempt(storage, user_id, document_id, token, cleaned=True)
        raise DocumentUploadError(CODE_UPLOAD_FAILED, _MSG_UPLOAD_FAILED) from exc
    return size, sha256


async def _drain_body(chunks: AsyncIterable[bytes], path: Path) -> tuple[int, str]:
    """Read the async body into one fixed private file, counting and hashing it.

    Empty pieces are ignored; the size limit is checked before a piece is
    written, so an oversized body is refused without storing the extra bytes and
    without reading further. The file is streamed in the pieces the caller sends
    and never held whole. The write and the close run in a worker thread so the
    event loop is not blocked by disk I/O.
    """
    digest = hashlib.sha256()
    size = 0
    handle = await asyncio.to_thread(path.open, 'wb')
    body_failed = False
    try:
        # One overall wall clock, spent only while waiting for the next piece: a
        # stalled body times out, while a disk write already in flight is never
        # cancelled halfway. Closing flushes, and it is only reached once every
        # write has actually finished.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RECEIVE_TIMEOUT_SECONDS
        iterator = chunks.__aiter__()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise DocumentUploadError(CODE_UPLOAD_TIMEOUT, _MSG_TIMEOUT)
            try:
                async with asyncio.timeout(remaining):
                    piece = await iterator.__anext__()
            except StopAsyncIteration:
                break
            if not piece:
                continue
            if size + len(piece) > MAX_FILE_BYTES:
                raise DocumentUploadError(CODE_TOO_LARGE, _MSG_TOO_LARGE)
            written = await _guarded(asyncio.to_thread(handle.write, piece))
            if written.cancelled:
                raise asyncio.CancelledError()
            if written.error is not None:
                raise written.error
            size += len(piece)
            digest.update(piece)
        if size == 0:
            raise DocumentUploadError(CODE_EMPTY, _MSG_EMPTY)
    except BaseException:
        body_failed = True
        raise
    finally:
        # Closing flushes the finished writes, and it only runs once every write
        # has actually ended, on a worker thread ``_guarded`` always drives to
        # its real end. A close failure after an otherwise complete receive is
        # this request's real failure; a body failure or cancellation already in
        # flight is never replaced by it.
        closed = await _guarded(asyncio.to_thread(handle.close))
        if not body_failed:
            if closed.cancelled:
                raise asyncio.CancelledError()
            if closed.error is not None:
                raise closed.error
    return size, digest.hexdigest()


async def _store_attempt(
    storage: PostgresStorage,
    *,
    user_id: int,
    document_id: str,
    token: str,
    key: str,
    knowledge_base_id: str,
    mime_type: str,
    path: Path,
    size: int,
    sha256: str,
) -> StoredDocument:
    """Store the finished private file under this attempt's key.

    ``store_document`` runs in a worker thread and cannot be cancelled, so a
    request cancellation is deferred until the thread has genuinely ended; only
    then is the attempt cleaned up or kept, and the cancellation re-raised. The
    returned key/size/digest are checked against our own received bytes before
    anything is published, and only our own key is ever removed - a foreign key
    handed back by the store is never deleted.
    """
    guarded = await _guarded(
        asyncio.to_thread(
            store_document,
            path=path,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            upload_token=token,
            mime_type=mime_type,
        )
    )
    error = guarded.error
    stored = guarded.result
    if error is None and not _matches(stored, key=key, size=size, sha256=sha256):
        error = RuntimeError(_MSG_MISMATCH)
    if error is not None:
        if isinstance(error, asyncio.CancelledError):
            # The transfer task itself was cancelled, so whether the object exists
            # is unknown; the journalled key is kept rather than deleted blind.
            cleaned = False
        else:
            cleaned = await _cleanup_own_key(key, error)
        if guarded.cancelled:
            await _fail_quietly(storage, user_id, document_id, token, cleaned=cleaned)
            raise asyncio.CancelledError()
        await _fail_attempt(storage, user_id, document_id, token, cleaned=cleaned)
        raise DocumentUploadError(CODE_UPLOAD_FAILED, _MSG_UPLOAD_FAILED) from error
    if guarded.cancelled:
        # The object exists at our own key but the request is gone and nothing was
        # published, so removing it and clearing the journal is the clean answer.
        cleaned = await _remove_own_key(key)
        await _fail_quietly(storage, user_id, document_id, token, cleaned=cleaned)
        raise asyncio.CancelledError()
    return stored


async def _publish(
    storage: PostgresStorage,
    *,
    user_id: int,
    document_id: str,
    token: str,
    key: str,
    size: int,
    sha256: str,
) -> dict[str, Any]:
    """Move the row to ``queued`` in one short transaction and return it.

    The finishing transaction is shielded: a cancellation is applied only after
    its real outcome is known, so this code never deletes an object that a
    committed ``queued`` row may already reference. An unknown commit error
    keeps the object, the journal and the row for a later metadata/recovery pass
    and reports ``persistence_failed`` - it is never reported as clean.
    """
    finished = await _guarded(_finish_attempt(storage, user_id, document_id, token, size, sha256))
    if finished.error is not None:
        raise DocumentUploadError(CODE_PERSISTENCE_FAILED, _MSG_PERSISTENCE_FAILED) from finished.error
    settled = await _guarded(_settle_attempt(storage, user_id, document_id, token, key, finished.result))
    if finished.cancelled or settled.cancelled:
        raise asyncio.CancelledError()
    if settled.error is not None:
        if isinstance(settled.error, DocumentUploadError):
            raise settled.error
        raise DocumentUploadError(CODE_PERSISTENCE_FAILED, _MSG_PERSISTENCE_FAILED) from settled.error
    return settled.result


async def _finish_attempt(
    storage: PostgresStorage,
    user_id: int,
    document_id: str,
    token: str,
    size: int,
    sha256: str,
) -> str:
    """``finish_upload`` inside its own short transaction; nothing else is committed."""
    async with storage.transaction():
        return await storage.knowledge.finish_upload(
            user_id,
            document_id,
            token,
            size_bytes=size,
            sha256=sha256,
        )


async def _settle_attempt(
    storage: PostgresStorage,
    user_id: int,
    document_id: str,
    token: str,
    key: str,
    outcome: str,
) -> dict[str, Any]:
    """Turn a settled finish outcome into either the queued row or an error.

    ``duplicate`` and ``stale`` remove only this attempt's own key and never
    revive the row: a ``queued``/``processing``/``ready`` row still referencing
    our key owns that object and it is left alone.
    """
    if outcome == 'duplicate':
        cleaned = await _remove_own_key(key)
        await _fail_attempt(
            storage,
            user_id,
            document_id,
            token,
            cleaned=cleaned,
            code=CODE_DUPLICATE,
            message=_MSG_DUPLICATE,
        )
        raise DocumentUploadError(CODE_DUPLICATE, _MSG_DUPLICATE)
    async with storage.transaction():
        current = await storage.knowledge.get_document(user_id, document_id)
    if outcome == 'stale':
        if not _references_published_key(current, key):
            await _remove_own_key(key)
        if current is None:
            raise DocumentUploadError(CODE_NOT_FOUND, _MSG_NOT_FOUND)
        raise DocumentUploadError(CODE_CONFLICT, _MSG_CONFLICT)
    if current is None:
        # The row was tombstoned between the commit and this read; it is gone.
        raise DocumentUploadError(CODE_NOT_FOUND, _MSG_NOT_FOUND)
    if current.get('state') not in _PUBLISHED_STATES:
        raise DocumentUploadError(CODE_CONFLICT, _MSG_CONFLICT)
    return current


# -- journal helpers ---------------------------------------------------------


async def _fail_attempt(
    storage: PostgresStorage,
    user_id: int,
    document_id: str,
    token: str,
    *,
    cleaned: bool,
    code: str = CODE_UPLOAD_FAILED,
    message: str = _MSG_UPLOAD_FAILED,
) -> None:
    """Mark this attempt ``failed`` in a short transaction.

    ``cleaned`` is a trusted bool: ``True`` only when a storage call confirmed
    our key is gone, so the journal may be cleared; ``False`` keeps the key for
    the later cleanup pass. If the database itself is unavailable the original
    journal is left alone and a generic ``persistence_failed`` is raised instead
    of fabricating a cleaned state.
    """
    try:
        async with storage.transaction():
            await storage.knowledge.fail_upload(
                user_id,
                document_id,
                token,
                error_code=code,
                error_message=message,
                cleanup_succeeded=cleaned,
            )
    except Exception as exc:
        raise DocumentUploadError(CODE_PERSISTENCE_FAILED, _MSG_PERSISTENCE_FAILED) from exc


async def _fail_quietly(
    storage: PostgresStorage,
    user_id: int,
    document_id: str,
    token: str,
    *,
    cleaned: bool,
) -> None:
    """Best-effort ``failed`` journal during a cancellation; the cancellation wins."""
    with contextlib.suppress(BaseException):
        await _fail_attempt(storage, user_id, document_id, token, cleaned=cleaned)


async def _remove_own_key(key: str) -> bool:
    """Delete exactly this attempt's key and report whether it is confirmed gone."""
    try:
        await asyncio.to_thread(delete_document_object, key)
    except Exception:
        return False
    return True


async def _cleanup_own_key(key: str, error: BaseException) -> bool:
    """Whether this attempt's object is confirmed absent after a transfer failure.

    ``store_document`` already removed the object itself for its own failures
    (and says whether that cleanup succeeded); any other failure is unknown, so
    this attempt's key is removed now and the confirmed result is returned.
    """
    if isinstance(error, DocumentStoreError):
        return not error.cleanup_failed
    return await _remove_own_key(key)


def _references_published_key(row: dict[str, Any] | None, key: str) -> bool:
    return row is not None and row.get('s3_key') == key and row.get('state') in _PUBLISHED_STATES


def _matches(stored: StoredDocument, *, key: str, size: int, sha256: str) -> bool:
    """Whether the store result describes exactly the bytes this attempt received."""
    return (
        getattr(stored, 'key', None) == key
        and getattr(stored, 'size_bytes', None) == size
        and getattr(stored, 'sha256', None) == sha256
    )


# -- cancellation-safe task handling -----------------------------------------


async def _guarded(coro: Awaitable[Any]) -> _Guarded:
    """Run ``coro`` as a task and wait for it even if this coroutine is cancelled.

    A cancellation never abandons the task: the task is always driven to
    completion, so its real outcome is known, and the cancellation is reported
    to the caller (which re-raises it once it has cleaned up). No fire-and-forget
    task is left behind.
    """
    task = asyncio.ensure_future(coro)
    cancelled = False
    while True:
        try:
            return _Guarded(result=await asyncio.shield(task), cancelled=cancelled)
        except asyncio.CancelledError:
            cancelled = True
            if task.cancelled():
                return _Guarded(error=asyncio.CancelledError(), cancelled=True)
            if not task.done():
                continue
            error = task.exception()
            if error is not None:
                return _Guarded(error=error, cancelled=True)
            return _Guarded(result=task.result(), cancelled=True)
        except Exception as exc:
            return _Guarded(error=exc, cancelled=cancelled)


__all__ = [
    'CODE_CONFLICT',
    'CODE_DUPLICATE',
    'CODE_EMPTY',
    'CODE_NOT_FOUND',
    'CODE_PERSISTENCE_FAILED',
    'CODE_TOO_LARGE',
    'CODE_UPLOAD_FAILED',
    'CODE_UPLOAD_TIMEOUT',
    'MAX_FILE_BYTES',
    'RECEIVE_TIMEOUT_SECONDS',
    'DocumentUploadError',
    'receive_document_file',
]
