"""Retrying one failed knowledge-base document.

This is the service half of ``POST /api/document/{id}/retry``: it re-checks the
owner and the state inside short transactions, then either re-queues a parse
failure or cleans up the object an upload failure journalled, and nothing else.
An upload failure whose object is confirmed gone and a parse failure both return
to a state the existing pipeline already understands (``pending_upload`` and
``queued``). Nothing is re-uploaded, no parse is started and no active
``uploading`` document is touched, so a 200 means the retry was *prepared*, not
that any file was received again.

The adapter owns the HTTP gate and the public projection; this module owns the
state decisions. Every cleanup decision is made from the row itself: client
input - a body, a key, a digest or a cleanup flag - never decides what is
deleted. The only key this module ever deletes is the exact one the row
journalled for the failed attempt, and only after the storage call confirmed it
is gone. Every object-store phase runs without a database transaction held.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from .store import delete_document_object
from .upload import (
    CODE_CONFLICT,
    CODE_NOT_FOUND,
    CODE_PERSISTENCE_FAILED,
    CODE_UPLOAD_FAILED,
    DocumentUploadError,
    _guarded,
)

if TYPE_CHECKING:
    from ..storage import PostgresStorage

# Fixed English sentences: storage text and private object keys stay on the
# ``__cause__`` chain for logs, never in a caller-facing message.
_MSG_NOT_FOUND = 'The document does not exist.'
_MSG_CONFLICT = 'The document cannot be retried in its current state.'
_MSG_UPLOAD_FAILED = 'The document could not be uploaded.'
_MSG_PERSISTENCE_FAILED = 'The retry result could not be confirmed.'


async def retry_document(
    *,
    storage: PostgresStorage,
    user_id: int,
    document_id: str,
) -> dict[str, Any]:
    """Prepare the retry of one owned, live, ``failed`` document.

    Returns the internal ``document`` row, which carries ``s3_key``/``sha256``
    and the lease columns, so an HTTP adapter must project it through its
    response whitelist and never return it as-is.

    ``sha256 IS NULL`` is an upload failure - not an empty-string or truthiness
    test - and anything else is a parse failure. A parse failure only runs the
    existing ``retry_import`` CAS and never deletes the source. An upload failure
    with no journalled key just re-queues the row; with one, exactly that key is
    deleted and confirmed gone before the row returns to ``pending_upload``, and
    a failed or unknown delete keeps the journal and the state.

    Raises :class:`DocumentUploadError` with a stable ASCII code for every
    failure, and re-raises cancellation only after the in-flight storage work has
    genuinely ended.
    """
    row = await _failed_document(storage, user_id, document_id)
    if row.get('sha256') is None:
        return await _retry_upload(storage, user_id, document_id, key=row.get('s3_key'))
    return await _retry_import(storage, user_id, document_id)


# -- phases ------------------------------------------------------------------


async def _failed_document(storage: PostgresStorage, user_id: int, document_id: str) -> dict[str, Any]:
    """The caller's live, ``failed`` row, read in a short transaction.

    Missing, malformed, foreign and tombstoned ids all answer ``not_found`` so
    the response never reveals whether the row exists; every other state is a
    ``conflict``. A database or commit failure during the read itself is a fixed
    ``persistence_failed`` instead - the row was never established - and the
    driver text stays on the cause. This runs before any object-store call and
    commits before the long phases, so no read transaction is left open.
    """
    row = await _read_row(storage, user_id, document_id)
    if row is None:
        raise DocumentUploadError(CODE_NOT_FOUND, _MSG_NOT_FOUND)
    if row.get('state') != 'failed':
        raise DocumentUploadError(CODE_CONFLICT, _MSG_CONFLICT)
    return row


async def _retry_import(storage: PostgresStorage, user_id: int, document_id: str) -> dict[str, Any]:
    """Re-queue a parse failure, keeping its uploaded source untouched.

    ``retry_import`` re-checks the complete source and the absence of a lease, so
    a source that was never fully uploaded (or a lease left behind) is a CAS miss
    and answers ``conflict`` - or ``not_found`` when the row disappeared. The
    shielded transaction means a cancellation never abandons a half-applied
    update, and an unknown commit result is never reported as clean.
    """
    settled = await _guarded(_retry_import_transaction(storage, user_id, document_id))
    if settled.cancelled:
        raise asyncio.CancelledError()
    if settled.error is not None:
        raise DocumentUploadError(CODE_PERSISTENCE_FAILED, _MSG_PERSISTENCE_FAILED) from settled.error
    if not settled.result:
        await _raise_conflict_or_gone(storage, user_id, document_id)
    return await _current_or_gone(storage, user_id, document_id)


async def _retry_import_transaction(storage: PostgresStorage, user_id: int, document_id: str) -> bool:
    async with storage.transaction():
        return await storage.knowledge.retry_import(user_id, document_id)


async def _retry_upload(
    storage: PostgresStorage,
    user_id: int,
    document_id: str,
    *,
    key: str | None,
) -> dict[str, Any]:
    """Return an upload failure to ``pending_upload``, cleaning a journalled key first.

    The key is the row's own value, never a caller's. It is removed through the
    same guarded worker-thread path the uploader uses, so a cancellation waits
    for the delete to really end before re-raising; a failed or unknown delete
    keeps the journal and the state and reports ``upload_failed``. Only after the
    delete is confirmed does the CAS run, and it must name that exact key, so a
    concurrent reset (or a new attempt's object) can never be overwritten.
    """
    if key is not None:
        await _remove_journalled_key(key)
    settled = await _guarded(_retry_upload_transaction(storage, user_id, document_id, key))
    if settled.cancelled:
        raise asyncio.CancelledError()
    if settled.error is not None:
        raise DocumentUploadError(CODE_PERSISTENCE_FAILED, _MSG_PERSISTENCE_FAILED) from settled.error
    if not settled.result:
        await _raise_conflict_or_gone(storage, user_id, document_id)
    return await _current_or_gone(storage, user_id, document_id)


async def _retry_upload_transaction(
    storage: PostgresStorage,
    user_id: int,
    document_id: str,
    key: str | None,
) -> bool:
    async with storage.transaction():
        return await storage.knowledge.retry_upload(user_id, document_id, cleaned_key=key)


async def _remove_journalled_key(key: str) -> None:
    """Delete exactly the failed attempt's key, or report a fixed retry failure.

    A real delete failure keeps the journal so a later retry can try again; the
    storage text stays on the cause. A cancellation is reported only once the
    delete thread has ended, and the caller then leaves the row as the failed
    attempt left it - the already-deleted key is harmless to delete again.
    """
    removed = await _guarded(asyncio.to_thread(delete_document_object, key))
    if removed.cancelled:
        raise asyncio.CancelledError()
    if removed.error is not None:
        raise DocumentUploadError(CODE_UPLOAD_FAILED, _MSG_UPLOAD_FAILED) from removed.error


# -- CAS outcomes ------------------------------------------------------------


async def _raise_conflict_or_gone(storage: PostgresStorage, user_id: int, document_id: str) -> None:
    """A lost CAS is ``not_found`` only when the latest owner/live row is gone."""
    if await _read_row(storage, user_id, document_id) is None:
        raise DocumentUploadError(CODE_NOT_FOUND, _MSG_NOT_FOUND)
    raise DocumentUploadError(CODE_CONFLICT, _MSG_CONFLICT)


async def _current_or_gone(storage: PostgresStorage, user_id: int, document_id: str) -> dict[str, Any]:
    row = await _read_row(storage, user_id, document_id)
    if row is None:
        raise DocumentUploadError(CODE_NOT_FOUND, _MSG_NOT_FOUND)
    return row


async def _read_row(storage: PostgresStorage, user_id: int, document_id: str) -> dict[str, Any] | None:
    """One owner-scoped read in its own short transaction.

    A database or commit failure means the read cannot be trusted, so it becomes
    the fixed ``persistence_failed`` and the SQL text stays on the cause. A
    cancellation is not an ``Exception``, so it is reported as itself and never
    rewritten as a persistence answer.
    """
    try:
        async with storage.transaction():
            return await storage.knowledge.get_document(user_id, document_id)
    except Exception as exc:
        raise DocumentUploadError(CODE_PERSISTENCE_FAILED, _MSG_PERSISTENCE_FAILED) from exc


__all__ = ['retry_document']
