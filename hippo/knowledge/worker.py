"""Importing one queued knowledge document, and the standalone worker loop.

This is the second half of the document state machine: it claims one ``queued``
row with a fresh server-side lease token, downloads the private object and
parses it on one worker thread, then either publishes ``ready`` with all its
chunks in a single short transaction, or records a fixed, safe failure and
leaves the uploaded source alone for a later retry. The object store is only
ever read here: no upload, no delete, no listing and no fabricated URL.

The queue is a plain poll - one document per cycle with a fixed backoff - plus
an entry point for running the process on its own; there is no job framework
and no new configuration. A claimed document is kept alive by one small
heartbeat task that opens its own short connection while the long download and
parse run outside any database transaction, and expired ``processing`` leases
are returned to the queue by :func:`recover_expired_imports` at startup and
then at most once per minute. A cancellation is only reported after the work it
was waiting for - the synchronous transfer and the heartbeat's short
transaction - has really ended.

Only ``processing`` import leases are reclaimed here, and the lease SQL has been
executed against SQLite only: real PostgreSQL visibility under a concurrent
heartbeat or requeue is not verified. The ``uploading`` journal still has no
reaper, so a crashed upload is not recovered by this worker and it must not be
deployed as a complete production recovery yet.

shortcut: an abandoned ``uploading`` row keeps its journalled key until a later
cleanup pass exists.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from time import monotonic

from ..logger import setup_logger
from ..storage import close_pool, open_storage
from ..utils import utc_now_dt
from .parse import DocumentParseError, ParsedDocument, parse_document
from .store import DocumentStoreError, download_document
from .upload import CODE_PERSISTENCE_FAILED, _guarded

logger = logging.getLogger(__name__)

#: The failure recorded when the download or the parse failed for a reason this
#: worker cannot name; the original exception stays on the cause chain.
CODE_IMPORT_FAILED = 'document.import_failed'
#: The failure recorded when the run was cancelled while it held the lease.
CODE_IMPORT_CANCELLED = 'document.import_cancelled'

_MSG_IMPORT_FAILED = 'The document could not be imported.'
_MSG_IMPORT_CANCELLED = 'The import was cancelled.'
_MSG_PERSISTENCE_FAILED = 'The import result could not be confirmed.'

#: Logged when the private working directory itself could not be removed. Fixed
#: text: the directory path and the OS error never reach a log record.
_CLEANUP_FAILED_LOG = 'the knowledge import working directory could not be removed'
#: Logged when a lease heartbeat could not reach the database. Fixed text: the
#: SQL/SDK error, the document id and the lease token never reach a log record.
_HEARTBEAT_FAILED_LOG = 'the knowledge import lease heartbeat failed'

#: Candidates read per cycle; listing is only a snapshot and every candidate is
#: still claimed by CAS, so a bounded batch keeps one transaction short.
_QUEUE_CANDIDATES = 20
#: Private working directory and fixed file name: no key, MIME or file name from
#: the row is ever concatenated into a local path.
_TEMP_DIRECTORY_PREFIX = 'hippo-knowledge-import-'
_SOURCE_FILE_NAME = 'source'
_WORKER_ID_PREFIX = 'knowledge-import'
_BACKOFF_SECONDS = 5.0

#: How often a claimed ``processing`` lease is refreshed while the long download
#: and the parser run. A fixed internal value, not a user option.
_HEARTBEAT_SECONDS = 30.0
#: A ``processing`` lease that no heartbeat refreshed for this long is expired.
_LEASE_STALE_SECONDS = 600.0
#: How often the standalone loop runs the recovery pass, in seconds.
_REAP_INTERVAL_SECONDS = 60.0
#: Expired leases reclaimed per recovery transaction; bounded so maintenance
#: stays one short commit.
_REAP_LIMIT = 50


class DocumentImportError(Exception):
    """An import failure carrying a stable ASCII ``code`` and a generic message.

    Only this worker's own persistence failures use it; a download or parse
    failure is recorded with that component's own code instead. Database and SDK
    text stays on the ``__cause__`` chain, never in ``str(error)``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class _Claim:
    """One document this worker owns for the lifetime of its lease."""

    user_id: int
    document_id: str
    lease_token: str
    s3_key: str
    size_bytes: int
    sha256: str
    mime_type: str


async def run_import_once(*, worker_id: str) -> bool:
    """Claim and process at most one queued document.

    ``False`` means no candidate could be claimed at all: an empty queue or a
    snapshot whose every entry lost its CAS. A claimed document answers ``True``
    even when the outcome was a recorded business failure or a late stale
    result, because the queue entry was really taken and finished with. A
    cancellation propagates instead of answering.
    """
    claim = await _claim_one(worker_id)
    if claim is None:
        return False
    await _process_claim(claim)
    return True


# -- phases ------------------------------------------------------------------


async def _claim_one(worker_id: str) -> _Claim | None:
    """List candidates and claim one of them in a single short transaction.

    Listing is not a claim: every candidate is re-checked by ``claim_import``
    under its own owner and a fresh server-side ``uuid4`` lease token, so a stale
    snapshot can never hand one document to two workers. The storage context ends
    here - connection released - before any temporary file or SDK call happens.
    """
    async with open_storage() as storage, storage.transaction():
        for candidate in await storage.knowledge.list_queued_imports(limit=_QUEUE_CANDIDATES):
            lease_token = str(uuid.uuid4())
            row = await storage.knowledge.claim_import(
                candidate['user_id'],
                candidate['id'],
                worker_id,
                lease_token,
            )
            if row is not None:
                return _Claim(
                    user_id=int(candidate['user_id']),
                    document_id=str(row['id']),
                    lease_token=lease_token,
                    s3_key=str(row['s3_key']),
                    size_bytes=int(row['size_bytes']),
                    sha256=str(row['sha256']),
                    mime_type=str(row['mime_type']),
                )
    return None


async def _process_claim(claim: _Claim) -> None:
    """Fetch and parse the claimed source in a private directory, then publish.

    The working file has a fixed name inside this run's own temporary directory,
    so neither the object key nor the stored file name reaches a local path. The
    synchronous download and parse run together on one worker thread while one
    heartbeat task renews the server-side lease through its own short
    transaction; the claim and the publish each open their own short connection
    too, so no database connection is held across the SDK or Poppler call. The
    heartbeat is always stopped and joined before the result is published or the
    directory is removed, so a cancellation that only lands while the heartbeat
    finishes can never turn into a published result. A directory that cannot be
    created or removed is this run's own fixed failure - never a parsed result or
    a cancellation silently dropped.
    """
    try:
        directory = tempfile.TemporaryDirectory(prefix=_TEMP_DIRECTORY_PREFIX)
    except OSError:
        # No working file was ever created, so this is a plain, nameable failure.
        if await _record_failure(claim, code=CODE_IMPORT_FAILED, message=_MSG_IMPORT_FAILED):
            logger.warning('one knowledge import failed with code %s', CODE_IMPORT_FAILED)
        return
    stop = asyncio.Event()
    heartbeat = asyncio.ensure_future(_heartbeat_loop(claim, stop))
    try:
        cancelled = False
        failure: tuple[str, str] | None = None
        try:
            parsed = await _fetch_and_parse(claim, Path(directory.name) / _SOURCE_FILE_NAME)
        except asyncio.CancelledError:
            cancelled = True
        except DocumentStoreError as exc:
            failure = (exc.code, exc.message)
        except DocumentParseError as exc:
            failure = (exc.code, exc.message)
        except Exception:
            # Unknown thread failure: a fixed code and sentence, because the
            # original may carry SQL, SDK or object-store text and a private path.
            failure = (CODE_IMPORT_FAILED, _MSG_IMPORT_FAILED)
        finally:
            # The long IO is over on every path: end the heartbeat and wait for
            # its short transaction to genuinely finish before publishing,
            # failing or removing the working directory. A cancellation arriving
            # here is recorded instead of letting a late result turn ready.
            stop.set()
            settled = await _guarded(heartbeat)
            if settled.cancelled:
                cancelled = True
        if cancelled:
            await _fail_quietly(claim)
            raise asyncio.CancelledError()
        if failure is not None:
            # A lost CAS is a normal stale result and is not even worth a line.
            if await _record_failure(claim, code=failure[0], message=failure[1]):
                logger.warning('one knowledge import failed with code %s', failure[0])
            return
    finally:
        # The removal is best-effort and only says this run leaked a temp file;
        # its own text may carry a private path, so it is logged fixed and never
        # allowed to replace a parsed result, a failure or a cancellation.
        try:
            directory.cleanup()
        except OSError:
            logger.warning(_CLEANUP_FAILED_LOG)
    await _publish(claim, parsed)


async def _heartbeat_loop(claim: _Claim, stop: asyncio.Event) -> None:
    """Refresh the claimed lease every ``_HEARTBEAT_SECONDS`` until ``stop``.

    Returns as soon as the lease is gone (a lost CAS), so the long IO is left to
    finish and the final publish/failure CAS decides the outcome. A database
    error is logged with one fixed sentence and retried on the next normal tick,
    never in a busy loop. The owning task always waits for this one to settle.
    """
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_SECONDS)
        except TimeoutError:
            pass
        else:
            return
        try:
            if not await _heartbeat_once(claim):
                return
        except Exception:
            logger.warning(_HEARTBEAT_FAILED_LOG)


async def _heartbeat_once(claim: _Claim) -> bool:
    """One lease refresh in its own short storage context and transaction."""
    async with open_storage() as storage, storage.transaction():
        return await storage.knowledge.heartbeat_import(
            claim.user_id,
            claim.document_id,
            claim.lease_token,
        )


async def _fetch_and_parse(claim: _Claim, path: Path) -> ParsedDocument:
    """Read the private object and parse it on one worker thread.

    Both steps are synchronous and cannot be interrupted, so ``_guarded`` waits
    for the real end of the thread even when this coroutine is cancelled. The
    recorded key, size and digest are passed as they are; the canonical MIME type
    decides the parser. A cancelled run never returns a parsed result: it is
    reported as a cancellation so the caller records the fixed cancelled code.
    """

    def _work() -> ParsedDocument:
        download_document(key=claim.s3_key, path=path, size_bytes=claim.size_bytes, sha256=claim.sha256)
        return parse_document(path, claim.mime_type)

    guarded = await _guarded(asyncio.to_thread(_work))
    if guarded.cancelled:
        raise asyncio.CancelledError()
    if guarded.error is not None:
        raise guarded.error
    return guarded.result


async def _publish(claim: _Claim, parsed: ParsedDocument) -> None:
    """Publish the whole parse result in one short transaction.

    ``complete_import`` moves the row to ``ready`` and replaces the chunks in a
    single transaction, so a half-published document cannot be observed. A lost
    CAS means the document was deleted or the lease moved on: the derived result
    is dropped, the source stays for a retry and no failure is invented. An
    unconfirmed commit reports a fixed persistence error while the possibly
    ``ready`` row and its source are left untouched, and a cancellation is only
    re-raised once the transaction's real outcome is known - so a committed
    publish never turns back into a failure.
    """
    outcome = await _guarded(_complete_transaction(claim, parsed))
    if isinstance(outcome.error, asyncio.CancelledError):
        raise outcome.error
    if outcome.error is not None:
        raise DocumentImportError(CODE_PERSISTENCE_FAILED, _MSG_PERSISTENCE_FAILED) from outcome.error
    if outcome.cancelled:
        raise asyncio.CancelledError()


async def _complete_transaction(claim: _Claim, parsed: ParsedDocument) -> bool:
    """The publishing CAS inside its own short transaction; nothing else commits."""
    async with open_storage() as storage, storage.transaction():
        return await storage.knowledge.complete_import(
            claim.user_id,
            claim.document_id,
            claim.lease_token,
            page_count=parsed.page_count,
            chunks=parsed.chunks,
        )


async def recover_expired_imports() -> int:
    """Requeue ``processing`` leases no heartbeat refreshed in time.

    One short transaction and one short storage context of its own; the caller
    decides how often this runs. Only the real reclaimed count is logged, with
    fixed text and a number - never a row id, key, hash or path.
    """
    cutoff = utc_now_dt() - timedelta(seconds=_LEASE_STALE_SECONDS)
    async with open_storage() as storage, storage.transaction():
        reclaimed = await storage.knowledge.requeue_expired_imports(locked_before=cutoff, limit=_REAP_LIMIT)
    if reclaimed:
        logger.info('reclaimed %d expired knowledge import lease(s)', reclaimed)
    return reclaimed


async def _record_failure(claim: _Claim, *, code: str, message: str) -> bool:
    """Mark the claimed document failed in a new short transaction.

    Returns whether this run's lease was the one that recorded the failure. A
    lost CAS is a normal stale result: a deletion or a newer lease has moved the
    row on, so nothing is rewritten and the derived result is dropped. The
    storage transaction is driven to its real end even if another cancellation
    arrives, and a database failure is reported as a fixed persistence error -
    never as a failure that was durably recorded.
    """
    outcome = await _guarded(_fail_transaction(claim, code=code, message=message))
    if isinstance(outcome.error, asyncio.CancelledError):
        raise outcome.error
    if outcome.error is not None:
        raise DocumentImportError(CODE_PERSISTENCE_FAILED, _MSG_PERSISTENCE_FAILED) from outcome.error
    if outcome.cancelled:
        raise asyncio.CancelledError()
    return bool(outcome.result)


async def _fail_transaction(claim: _Claim, *, code: str, message: str) -> bool:
    """The failure CAS inside its own short transaction; the source is kept."""
    async with open_storage() as storage, storage.transaction():
        return await storage.knowledge.fail_import(
            claim.user_id,
            claim.document_id,
            claim.lease_token,
            error_code=code,
            error_message=message,
        )


async def _fail_quietly(claim: _Claim) -> None:
    """Best-effort cancelled marker while settling a cancellation.

    The cancellation always wins: whatever this short transaction answers (or
    fails with) is not allowed to replace it, and the caller re-raises.
    """
    with contextlib.suppress(BaseException):
        await _record_failure(claim, code=CODE_IMPORT_CANCELLED, message=_MSG_IMPORT_CANCELLED)


# -- standalone process ------------------------------------------------------


def _new_worker_id() -> str:
    """One ASCII process label, generated once per process."""
    return f'{_WORKER_ID_PREFIX}-{uuid.uuid4().hex}'


async def run_import_worker(*, worker_id: str) -> None:
    """Poll the queue forever, one document per cycle, with a fixed backoff.

    The recovery pass runs once at startup and then at most once per
    ``_REAP_INTERVAL_SECONDS``; a single blocked job is followed by another check
    on the next cycle. It shares the loop's fixed backoff, so an idle queue and a
    failed cycle are still maintained without busy-looping. ``CancelledError`` is
    not an ``Exception``, so it leaves this loop instead of being logged and
    retried, and the connection pool is closed in ``finally`` on every exit. Only
    fixed English text is logged: no object key, hash, local path, document body
    or raw exception ever reaches a log record.
    """
    try:
        last_reap = -_REAP_INTERVAL_SECONDS
        while True:
            try:
                now = monotonic()
                if now - last_reap >= _REAP_INTERVAL_SECONDS:
                    # Mark the interval before the call so a failing database is
                    # retried on the loop's fixed backoff, not in a busy loop.
                    last_reap = now
                    await recover_expired_imports()
                worked = await run_import_once(worker_id=worker_id)
            except Exception:
                # The exception's own text may carry SQL, SDK text or a path.
                logger.warning('knowledge import cycle failed; backing off')
                await asyncio.sleep(_BACKOFF_SECONDS)
                continue
            if not worked:
                logger.debug('knowledge import queue is idle')
                await asyncio.sleep(_BACKOFF_SECONDS)
    finally:
        await close_pool()


def main() -> None:
    """Run the standalone worker: ``python -m hippo.knowledge.worker``.

    Only this entry point installs the logging pipeline, from ``config.toml``
    like every other process (level, format and the ``{service}`` file target);
    importing this module never reads the configuration or starts a sink.
    """
    setup_logger(__name__, service='hippo-knowledge-worker')
    worker_id = _new_worker_id()
    logger.info('knowledge import worker %s starting', worker_id)
    # Ctrl-C is a clean stop: the loop's ``finally`` already closed the pool.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_import_worker(worker_id=worker_id))


if __name__ == '__main__':
    main()


__all__ = [
    'CODE_IMPORT_CANCELLED',
    'CODE_IMPORT_FAILED',
    'DocumentImportError',
    'main',
    'recover_expired_imports',
    'run_import_once',
    'run_import_worker',
]
