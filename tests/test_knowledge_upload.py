"""Raw upload reception: the state machine, the private file and cancellation.

The upload service runs over the real ``KnowledgeRepository`` on the in-memory
SQLite double borrowed from ``test_knowledge_repository``, with a storage handle
whose ``transaction()`` really commits and rolls back. The object store is a
synchronous in-process fake of ``store_document``/``delete_document_object``:
no SDK, no network, real hashing of the small private file. The HTTP layer, the
import worker and an expiry reaper do not exist yet, so nothing here covers
them.

What is *not* covered: real PostgreSQL transactions and row locks, real S3
uploads, process death and multi-cancellation. Those need a deployment
environment; these mocks are not an upload acceptance test.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import tempfile
import threading
import unittest
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from test_knowledge_repository import _AsyncConnection, _Harness

from hippo.knowledge import upload as upload_module
from hippo.knowledge.store import DocumentStoreError, StoredDocument, build_document_key
from hippo.knowledge.upload import (
    CODE_CONFLICT,
    CODE_DUPLICATE,
    CODE_EMPTY,
    CODE_NOT_FOUND,
    CODE_PERSISTENCE_FAILED,
    CODE_TOO_LARGE,
    CODE_UPLOAD_FAILED,
    CODE_UPLOAD_TIMEOUT,
    DocumentUploadError,
    receive_document_file,
)
from hippo.repositories.knowledge import KnowledgeRepository

#: Every blocking wait in this file is bounded by this; nothing asserts on speed.
WAIT_SECONDS = 5.0

#: Small payloads only: the size limit is patched down rather than allocating 64 MiB.
PAYLOAD = b'%PDF-1.7\n' + b'body' * 512


class _TxConnection(_AsyncConnection):
    """The SQLite double plus the commit/rollback ``storage.transaction()`` calls."""

    async def commit(self) -> None:
        self._conn.commit()

    async def rollback(self) -> None:
        self._conn.rollback()


class _FakeStorage:
    """The slice of ``PostgresStorage`` this service uses.

    ``transaction()`` commits and rolls back for real (as the API tests do), and
    counts how many transactions are open at once, so a service that held one
    across the body or the transfer would be caught.
    """

    def __init__(self, conn: _TxConnection) -> None:
        self._conn = conn
        self.knowledge = KnowledgeRepository(conn)  # type: ignore[arg-type]
        self.commits = 0
        self.rollbacks = 0
        self.active = 0
        self.max_active = 0
        #: 1-based commit ordinal that physically commits but never reports back.
        self.unknown_commit_at: int | None = None
        #: Set (from whichever thread) when a transfer or a read is being consumed.
        self.active_at_transfer: int | None = None

    @asynccontextmanager
    async def transaction(self):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            yield self
        except BaseException:
            self.rollbacks += 1
            await self._conn.rollback()
            raise
        else:
            await self._conn.commit()
            self.commits += 1
            if self.unknown_commit_at is not None and self.commits == self.unknown_commit_at:
                # The database committed; the confirmation was lost on the way back.
                raise RuntimeError('commit outcome unknown')
        finally:
            self.active -= 1


class _UploadHarness(_Harness):
    """Repository harness whose connection the storage handle also commits through."""

    def __init__(self) -> None:
        super().__init__()
        self.connection = _TxConnection(self.raw)
        self.repo = KnowledgeRepository(self.connection)  # type: ignore[arg-type]
        self.storage = _FakeStorage(self.connection)


class _FakeS3:
    """Synchronous stand-in for ``store_document``/``delete_document_object``.

    A successful "store" hashes and measures the private file for real, so the
    row's size and SHA-256 can be compared with the bytes the fake service
    received. ``on_call`` runs on the worker thread the service handed the store
    to, which is what lets a test hold a transfer open and cancel around it.
    """

    def __init__(
        self,
        storage: _FakeStorage,
        *,
        error: BaseException | None = None,
        key_override: str | None = None,
        size_override: int | None = None,
        sha_override: str | None = None,
        delete_error: BaseException | None = None,
        on_call=None,
        on_delete=None,
    ) -> None:
        self.storage = storage
        self.error = error
        self.key_override = key_override
        self.size_override = size_override
        self.sha_override = sha_override
        self.delete_error = delete_error
        self.on_call = on_call
        self.on_delete = on_delete
        self.calls: list[dict] = []
        self.deletes: list[str] = []
        self.caller_is_thread = False

    def store_document(
        self,
        *,
        path: Path,
        knowledge_base_id: str,
        document_id: str,
        upload_token: str,
        mime_type: str,
    ) -> StoredDocument:
        self.calls.append(
            {
                'path': Path(path),
                'knowledge_base_id': knowledge_base_id,
                'document_id': document_id,
                'upload_token': upload_token,
                'mime_type': mime_type,
            }
        )
        # The service must not hold a transaction while this runs.
        self.storage.active_at_transfer = self.storage.active
        self.caller_is_thread = threading.current_thread() is not threading.main_thread()
        if self.on_call is not None:
            self.on_call(Path(path))
        if self.error is not None:
            raise self.error
        # The double hashes the private file for real; it reads it directly rather
        # than through ``Path.read_bytes``, which one test patches to catch the
        # service itself slurping the body.
        with Path(path).open('rb') as handle:
            payload = handle.read()
        key = build_document_key(
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            upload_token=upload_token,
            mime_type=mime_type,
        )
        return StoredDocument(
            key=self.key_override or key,
            size_bytes=len(payload) if self.size_override is None else self.size_override,
            sha256=hashlib.sha256(payload).hexdigest() if self.sha_override is None else self.sha_override,
        )

    def delete_document_object(self, key: str) -> None:
        self.deletes.append(key)
        if self.on_delete is not None:
            self.on_delete(key)
        if self.delete_error is not None:
            raise self.delete_error


class _ChunkStream:
    """An async byte stream that records when and how it was consumed."""

    def __init__(
        self,
        storage: _FakeStorage,
        executed: list[str],
        pieces: list[bytes],
        *,
        error: BaseException | None = None,
        stall: asyncio.Event | None = None,
        first_pull: asyncio.Event | None = None,
    ) -> None:
        self.storage = storage
        self.executed = executed
        self.pieces = list(pieces)
        self.error = error
        self.stall = stall
        self.first_pull = first_pull
        self.pulled = 0
        self.active_seen: list[int] = []
        self.executed_at_first_pull: int | None = None

    async def __aiter__(self):
        for piece in self.pieces:
            if self.pulled == 0:
                self.executed_at_first_pull = len(self.executed)
                if self.first_pull is not None:
                    self.first_pull.set()
            self.pulled += 1
            self.active_seen.append(self.storage.active)
            yield piece
        if self.stall is not None:
            # A bounded wait: the service's own deadline must fire first, and a
            # broken service still cannot hang the suite forever.
            with suppress(TimeoutError):
                await asyncio.wait_for(self.stall.wait(), WAIT_SECONDS)
        if self.error is not None:
            raise self.error


class _FailingClose:
    """A private-file double whose close really closes and may then report a failure.

    ``on_close`` runs on the worker thread inside the close, so a test can hold
    the close open; the real handle is always closed first, so a leaked file
    descriptor cannot be what the test is observing.
    """

    def __init__(self, handle, error: BaseException | None, on_close=None) -> None:
        self._handle = handle
        self._error = error
        self._on_close = on_close

    def write(self, data: bytes) -> int:
        return self._handle.write(data)

    def close(self) -> None:
        self._handle.close()
        if self._on_close is not None:
            self._on_close()
        if self._error is not None:
            raise self._error

    def __getattr__(self, name: str):
        return getattr(self._handle, name)


class _UploadTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.h = _UploadHarness()
        self.s3 = _FakeS3(self.h.storage)
        self.temp_dirs: list[tempfile.TemporaryDirectory] = []
        self._real_tempdir = tempfile.TemporaryDirectory
        self.transfer_release: threading.Event | None = None
        for target, name, replacement in (
            (upload_module, 'store_document', self.s3.store_document),
            (upload_module, 'delete_document_object', self.s3.delete_document_object),
            (upload_module.tempfile, 'TemporaryDirectory', self._record_tempdir),
        ):
            patched = patch.object(target, name, new=replacement)
            patched.start()
            self.addCleanup(patched.stop)
        self.base1 = await self.h.repo.create_knowledge_base(1, 'Mine')
        self.base2 = await self.h.repo.create_knowledge_base(2, 'Theirs')

    async def asyncTearDown(self) -> None:
        self.h.raw.close()

    def _record_tempdir(self, **kwargs):
        directory = self._real_tempdir(**kwargs)
        self.temp_dirs.append(directory)
        return directory

    def temp_path(self) -> Path:
        """The one private working directory the service created."""
        self.assertEqual(1, len(self.temp_dirs))
        return Path(self.temp_dirs[0].name)

    async def seed_document(self, *, owner: int = 1, mime: str = 'application/pdf', filename: str = 'a.pdf') -> dict:
        base = self.base1 if owner == 1 else self.base2
        return await self.h.repo.create_document(owner, base['id'], filename=filename, mime_type=mime)

    def stream(self, pieces: list[bytes], **options) -> _ChunkStream:
        return _ChunkStream(self.h.storage, self.h.executed, pieces, **options)

    def patch_close(self, *, error: BaseException | None = None, on_close=None) -> None:
        """Make the private file's ``wb`` close report ``error`` after really closing.

        Only ``wb`` is wrapped; every other mode delegates to the real
        ``Path.open``, so the store double keeps reading the file untouched.
        """
        real_open = Path.open

        def opening(path: Path, mode: str = 'r', *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if mode != 'wb':
                return handle
            return _FailingClose(handle, error, on_close)

        patched = patch.object(Path, 'open', opening)
        patched.start()
        self.addCleanup(patched.stop)

    async def receive(self, document_id: str, chunks, *, owner: int = 1) -> dict:
        return await receive_document_file(
            storage=self.h.storage,
            user_id=owner,
            document_id=document_id,
            chunks=chunks,
        )

    async def failed_receive(self, document_id: str, chunks, *, owner: int = 1) -> DocumentUploadError:
        with self.assertRaises(DocumentUploadError) as caught:
            await self.receive(document_id, chunks, owner=owner)
        return caught.exception

    async def hold_transfer(self, document: dict) -> asyncio.Task:
        """Start a receive whose transfer thread is held until released.

        The object store runs on a worker thread, so the test mutates the SQLite
        row on this thread while that thread is parked; the private file, the
        lease and the row state are all observable at that point.
        """
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        self.transfer_release = release

        def _hold(_path: Path) -> None:
            entered.set()
            self.assertTrue(release.wait(timeout=WAIT_SECONDS))

        self.s3.on_call = _hold
        task = asyncio.ensure_future(self.receive(document['id'], self.stream([b'payload'])))
        self.assertTrue(await asyncio.to_thread(entered.wait, WAIT_SECONDS))
        return task

    async def finish_transfer(self, task: asyncio.Task) -> DocumentUploadError:
        """Release the held transfer and return the failure the receive raises."""
        self.transfer_release.set()
        with self.assertRaises(DocumentUploadError) as caught:
            await task
        return caught.exception


class AuthorizationTest(_UploadTestCase):
    """The owner and the state are settled before any body, temp or S3 work."""

    async def test_foreign_missing_and_malformed_documents_never_read_the_body(self) -> None:
        document = await self.seed_document()
        for owner, document_id in ((2, document['id']), (1, str(uuid4())), (1, 'not-a-uuid'), (1, None)):
            stream = self.stream([b'payload'])
            error = await self.failed_receive(document_id, stream, owner=owner)
            self.assertEqual(CODE_NOT_FOUND, error.code)
            self.assertEqual(0, stream.pulled)
        self.assertEqual([], self.s3.calls)
        self.assertEqual([], self.s3.deletes)
        # Only the owner-scoped read ran: no state, journal, S3 or workspace work.
        self.assertFalse(any('UPDATE' in sql for sql in self.h.executed))
        self.assertEqual('pending_upload', self.h.document_row(document['id'])['state'])

    async def test_deleted_documents_are_not_found_and_other_states_conflict(self) -> None:
        document = await self.seed_document()
        for state in ('uploading', 'queued', 'processing', 'ready', 'failed', 'unsupported'):
            self.h.raw.execute('UPDATE document SET state = ? WHERE id = ?', (state, document['id']))
            self.h.raw.commit()
            stream = self.stream([b'payload'])
            error = await self.failed_receive(document['id'], stream)
            self.assertEqual(CODE_CONFLICT, error.code, state)
            self.assertEqual(0, stream.pulled, state)

        self.h.raw.execute(
            "UPDATE document SET state = 'deleted', deleted_at = ? WHERE id = ?",
            ('2020-01-01T00:00:00+00:00', document['id']),
        )
        self.h.raw.commit()
        stream = self.stream([b'payload'])
        self.assertEqual(CODE_NOT_FOUND, (await self.failed_receive(document['id'], stream)).code)
        self.assertEqual(0, stream.pulled)
        self.assertEqual([], self.s3.calls)
        self.assertEqual([], self.s3.deletes)

    async def test_the_method_takes_no_client_token_key_digest_or_file_name(self) -> None:
        parameters = set(inspect.signature(receive_document_file).parameters)
        self.assertEqual({'storage', 'user_id', 'document_id', 'chunks'}, parameters)

        document = await self.seed_document(filename='private client name.pdf')
        row = await self.receive(document['id'], self.stream([b'payload']))
        # The key is built only from the row's own canonical ids and a fresh
        # server-side uuid4 attempt token; nothing from the caller or the file name.
        prefix, base_id, doc_id, name = row['s3_key'].split('/')
        self.assertEqual(('knowledge', row['knowledge_base_id'], row['id']), (prefix, base_id, doc_id))
        self.assertTrue(name.endswith('.pdf'))
        attempt_token = name[: -len('.pdf')]
        self.assertEqual(attempt_token, str(UUID(attempt_token)))
        self.assertNotIn('private', row['s3_key'])
        self.assertNotIn('client', row['s3_key'])
        self.assertEqual('queued', row['state'])
        self.assertNotEqual(str(uuid4()), attempt_token)

    async def test_the_attempt_token_is_journalled_before_the_body_is_stored(self) -> None:
        document = await self.seed_document()
        stream = self.stream([b'payload'])
        row = await self.receive(document['id'], stream)
        # The CAS that writes the token and the key ran before the first chunk.
        first_pull = stream.executed_at_first_pull
        self.assertIsNotNone(first_pull)
        issued = ' '.join(self.h.executed[:first_pull])
        self.assertIn("SET state = 'uploading'", issued)
        self.assertIn('lease_token IS NULL', issued)
        # Four short transactions on the success path: the owner read, the begin
        # journal, the finishing update and the post-finish row read; none of
        # them spans the body or the transfer.
        self.assertEqual(4, self.h.storage.commits)
        self.assertEqual(0, self.h.storage.rollbacks)
        self.assertEqual(1, self.h.storage.max_active)
        self.assertEqual('queued', row['state'])


class ReceiveTest(_UploadTestCase):
    """The body is streamed to a private fixed-name file with real hashing."""

    async def test_the_real_size_and_digest_reach_the_queued_row(self) -> None:
        document = await self.seed_document(filename='secret-client-name.pdf')
        private_files: list[list[str]] = []
        self.s3.on_call = lambda path: private_files.append(sorted(item.name for item in path.parent.iterdir()))
        stream = self.stream([b'%PDF-1.7\n', b'body' * 512, b''])
        # Any whole-file read would be recorded here; the body must be streamed.
        whole_reads: list[str] = []
        real_read_bytes = Path.read_bytes
        with patch.object(
            Path,
            'read_bytes',
            lambda self: whole_reads.append(str(self)) or real_read_bytes(self),
        ):
            row = await self.receive(document['id'], stream)

        self.assertEqual([], whole_reads)
        self.assertEqual('queued', row['state'])
        self.assertEqual(len(PAYLOAD), row['size_bytes'])
        self.assertEqual(hashlib.sha256(PAYLOAD).hexdigest(), row['sha256'])
        self.assertEqual('queued', self.h.document_row(document['id'])['state'])
        self.assertIsNone(row['lease_token'])
        self.assertIsNone(row['locked_by'])
        # The private file is fixed-name inside a private directory, never the
        # client's file name, and the store is asked for nothing else.
        self.assertEqual([['document']], private_files)
        self.assertEqual(
            {'path', 'knowledge_base_id', 'document_id', 'upload_token', 'mime_type'},
            set(self.s3.calls[0]),
        )
        self.assertEqual('document', self.s3.calls[0]['path'].name)
        self.assertTrue(self.temp_path().name.startswith('hippo-knowledge-upload-'))
        self.assertFalse(self.temp_path().exists())
        self.assertTrue(self.s3.caller_is_thread)

    async def test_the_body_is_consumed_without_a_transaction_and_the_temp_is_removed(self) -> None:
        document = await self.seed_document()
        stream = self.stream([b'a', b'b', b'c'])
        await self.receive(document['id'], stream)

        self.assertEqual(3, stream.pulled)
        self.assertEqual([0, 0, 0], stream.active_seen)
        self.assertEqual(0, self.h.storage.active_at_transfer)
        self.assertEqual(1, self.h.storage.max_active)
        self.assertFalse(self.temp_path().exists())

    async def test_an_empty_body_fails_and_clears_the_journal(self) -> None:
        document = await self.seed_document()
        stream = self.stream([b'', b''])
        error = await self.failed_receive(document['id'], stream)

        self.assertEqual(CODE_EMPTY, error.code)
        self.assertEqual('The document is empty.', str(error))
        self.assertEqual([], self.s3.calls)
        row = self.h.document_row(document['id'])
        self.assertEqual('failed', row['state'])
        self.assertIsNone(row['s3_key'])
        self.assertIsNone(row['lease_token'])
        self.assertEqual(CODE_EMPTY, row['error_code'])
        self.assertFalse(self.temp_path().exists())

    async def test_a_body_over_the_limit_is_refused_before_the_piece_is_written(self) -> None:
        limit = 8
        document = await self.seed_document()
        stream = self.stream([b'12345678', b'9'])
        with patch.object(upload_module, 'MAX_FILE_BYTES', limit):
            error = await self.failed_receive(document['id'], stream)

        self.assertEqual(CODE_TOO_LARGE, error.code)
        self.assertEqual(2, stream.pulled)
        self.assertEqual([], self.s3.calls)
        row = self.h.document_row(document['id'])
        self.assertEqual('failed', row['state'])
        self.assertIsNone(row['s3_key'])
        self.assertEqual(CODE_TOO_LARGE, row['error_code'])
        self.assertFalse(self.temp_path().exists())

    async def test_a_failing_iterator_fails_cleanly_and_hides_its_text(self) -> None:
        document = await self.seed_document()
        stream = self.stream([b'partial'], error=RuntimeError('body socket broke'))
        error = await self.failed_receive(document['id'], stream)

        self.assertEqual(CODE_UPLOAD_FAILED, error.code)
        self.assertNotIn('socket', str(error))
        self.assertNotIn('body socket broke', str(error))
        self.assertIsInstance(error.__cause__, RuntimeError)
        self.assertEqual([], self.s3.calls)
        row = self.h.document_row(document['id'])
        self.assertEqual('failed', row['state'])
        self.assertIsNone(row['s3_key'])
        self.assertEqual(CODE_UPLOAD_FAILED, row['error_code'])
        self.assertFalse(self.temp_path().exists())

    async def test_a_stalled_body_hits_the_wall_clock_and_never_reaches_the_store(self) -> None:
        document = await self.seed_document()
        stream = self.stream([b'partial'], stall=asyncio.Event())
        with patch.object(upload_module, 'RECEIVE_TIMEOUT_SECONDS', 0.05):
            error = await self.failed_receive(document['id'], stream)

        self.assertEqual(CODE_UPLOAD_TIMEOUT, error.code)
        self.assertEqual('The upload timed out.', str(error))
        self.assertEqual([], self.s3.calls)
        row = self.h.document_row(document['id'])
        self.assertEqual('failed', row['state'])
        self.assertIsNone(row['s3_key'])
        self.assertEqual(CODE_UPLOAD_TIMEOUT, row['error_code'])
        self.assertFalse(self.temp_path().exists())


class CloseFailureTest(_UploadTestCase):
    """A close failure is real: reported after a complete body, never overriding one."""

    async def test_a_close_failure_after_a_complete_body_is_the_reported_failure(self) -> None:
        document = await self.seed_document()
        close_error = OSError('flush failed: /private/tmp/document')
        self.patch_close(error=close_error)
        error = await self.failed_receive(document['id'], self.stream([b'payload']))

        self.assertEqual(CODE_UPLOAD_FAILED, error.code)
        self.assertIs(close_error, error.__cause__)
        # Closing failed, so the received bytes were never handed to the store.
        self.assertEqual([], self.s3.calls)
        self.assertEqual([], self.s3.deletes)
        row = self.h.document_row(document['id'])
        self.assertEqual('failed', row['state'])
        self.assertIsNone(row['s3_key'])
        self.assertIsNone(row['sha256'])
        self.assertEqual(CODE_UPLOAD_FAILED, row['error_code'])
        self.assertFalse(self.temp_path().exists())

    async def test_a_repeated_cancel_waits_for_a_blocked_close_before_cleaning_up(self) -> None:
        document = await self.seed_document()
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def _hold_close() -> None:
            entered.set()
            # Bounded, so a broken service cannot hang the suite; the outcome is
            # checked on the event loop rather than inside this worker thread.
            release.wait(timeout=WAIT_SECONDS)

        close_error = OSError('flush failed: /private/tmp/document')
        self.patch_close(error=close_error, on_close=_hold_close)
        task = asyncio.ensure_future(self.receive(document['id'], self.stream([b'payload'])))
        self.assertTrue(await asyncio.to_thread(entered.wait, WAIT_SECONDS))
        temp = self.temp_path()
        self.assertTrue(temp.exists())

        # The close is still running on the worker thread, so the private file must
        # survive and the task must not settle - however often it is cancelled.
        for _ in range(2):
            task.cancel()
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
            self.assertTrue(temp.exists())
            self.assertFalse(task.done())
        self.assertEqual([], self.s3.calls)

        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

        # The close failure was raised but the cancellation already in flight won.
        self.assertFalse(temp.exists())
        row = self.h.document_row(document['id'])
        self.assertEqual('failed', row['state'])
        self.assertIsNone(row['s3_key'])
        self.assertIsNone(row['lease_token'])

    async def test_a_close_failure_does_not_replace_a_body_failure(self) -> None:
        document = await self.seed_document()
        body_error = RuntimeError('body socket broke')
        self.patch_close(error=OSError('flush failed: /private/tmp/document'))
        error = await self.failed_receive(document['id'], self.stream([b'partial'], error=body_error))

        # The body failure is the real cause; the close failure is not reported at all.
        self.assertEqual(CODE_UPLOAD_FAILED, error.code)
        self.assertIs(body_error, error.__cause__)
        self.assertNotIn('flush failed', str(error))
        self.assertEqual([], self.s3.calls)
        row = self.h.document_row(document['id'])
        self.assertEqual('failed', row['state'])
        self.assertIsNone(row['s3_key'])
        self.assertIsNone(row['sha256'])
        self.assertFalse(self.temp_path().exists())


class StoreTest(_UploadTestCase):
    """Only this attempt's object is ever removed, and only after a real answer."""

    async def test_a_confirmed_store_failure_clears_the_journal(self) -> None:
        document = await self.seed_document()
        self.s3.error = DocumentStoreError('document.upload_failed', 'The document could not be stored.')
        error = await self.failed_receive(document['id'], self.stream([b'payload']))

        self.assertEqual(CODE_UPLOAD_FAILED, error.code)
        self.assertEqual('The document could not be uploaded.', str(error))
        self.assertNotIn('could not be stored', str(error))
        # store_document already removed its own key and said so, so the journal
        # may be cleared without a second delete.
        self.assertEqual([], self.s3.deletes)
        row = self.h.document_row(document['id'])
        self.assertEqual('failed', row['state'])
        self.assertIsNone(row['s3_key'])
        self.assertFalse(self.temp_path().exists())

    async def test_an_unknown_transfer_failure_removes_only_this_attempt_key(self) -> None:
        document = await self.seed_document()
        self.s3.error = RuntimeError('SDKUploadDenied: s3://private/bucket')
        error = await self.failed_receive(document['id'], self.stream([b'payload']))

        self.assertEqual(CODE_UPLOAD_FAILED, error.code)
        self.assertNotIn('SDKUploadDenied', str(error))
        self.assertNotIn('s3://', str(error))
        self.assertIsInstance(error.__cause__, RuntimeError)
        # The unknown failure meant the service could not assume the object was
        # gone, so it removed exactly the key its own attempt journalled.
        own_key = build_document_key(
            **{
                name: self.s3.calls[0][name]
                for name in ('knowledge_base_id', 'document_id', 'upload_token', 'mime_type')
            }
        )
        self.assertEqual([own_key], self.s3.deletes)
        row = self.h.document_row(document['id'])
        self.assertEqual('failed', row['state'])
        # Cleanup was confirmed, so the key does not stay journalled.
        self.assertIsNone(row['s3_key'])

    async def test_a_failed_cleanup_keeps_the_key_journalled(self) -> None:
        document = await self.seed_document()
        self.s3.error = RuntimeError('upload failed')
        self.s3.delete_error = DocumentStoreError('document.delete_failed', 'The stored document could not be deleted.')
        error = await self.failed_receive(document['id'], self.stream([b'payload']))

        self.assertEqual(CODE_UPLOAD_FAILED, error.code)
        row = self.h.document_row(document['id'])
        self.assertEqual('failed', row['state'])
        # The object may still exist, so its key stays for the later cleanup pass.
        self.assertEqual([row['s3_key']], self.s3.deletes)
        self.assertIsNotNone(row['s3_key'])
        self.assertEqual(CODE_UPLOAD_FAILED, row['error_code'])

    async def test_a_foreign_result_is_never_published_and_a_foreign_key_never_deleted(self) -> None:
        document = await self.seed_document()
        foreign = build_document_key(
            knowledge_base_id=document['knowledge_base_id'],
            document_id=str(uuid4()),
            upload_token=str(uuid4()),
            mime_type='application/pdf',
        )
        self.s3.key_override = foreign
        error = await self.failed_receive(document['id'], self.stream([b'payload']))

        self.assertEqual(CODE_UPLOAD_FAILED, error.code)
        self.assertNotIn(foreign, str(error))
        # Only our own key is cleaned: it is the one the service itself built and
        # journalled, never the foreign key the store handed back.
        own_key = build_document_key(
            **{
                name: self.s3.calls[0][name]
                for name in ('knowledge_base_id', 'document_id', 'upload_token', 'mime_type')
            }
        )
        self.assertEqual([own_key], self.s3.deletes)
        self.assertNotEqual(foreign, own_key)
        self.assertEqual('failed', self.h.document_row(document['id'])['state'])

    async def test_a_wrong_size_or_digest_from_the_store_is_refused(self) -> None:
        document = await self.seed_document()
        self.s3.size_override = len(PAYLOAD) + 1
        self.assertEqual(
            CODE_UPLOAD_FAILED,
            (await self.failed_receive(document['id'], self.stream([b'payload']))).code,
        )
        self.assertEqual('failed', self.h.document_row(document['id'])['state'])
        self.assertIsNone(self.h.document_row(document['id'])['size_bytes'])

        other = await self.seed_document()
        self.s3.size_override = None
        self.s3.sha_override = 'a' * 64
        self.assertEqual(CODE_UPLOAD_FAILED, (await self.failed_receive(other['id'], self.stream([b'payload']))).code)
        self.assertEqual('failed', self.h.document_row(other['id'])['state'])
        self.assertIsNone(self.h.document_row(other['id'])['sha256'])


class DuplicateAndDeletionTest(_UploadTestCase):
    """Dedupe, deletion and superseded attempts clean up only this attempt's key."""

    async def test_a_duplicate_digest_fails_the_loser_and_keeps_the_winner_object(self) -> None:
        winner = await self.seed_document()
        won = await self.receive(winner['id'], self.stream([PAYLOAD]))
        loser = await self.seed_document()
        error = await self.failed_receive(loser['id'], self.stream([PAYLOAD]))

        self.assertEqual(CODE_DUPLICATE, error.code)
        self.assertEqual('queued', self.h.document_row(winner['id'])['state'])
        self.assertEqual(won['s3_key'], self.h.document_row(winner['id'])['s3_key'])
        row = self.h.document_row(loser['id'])
        self.assertEqual('failed', row['state'])
        self.assertIsNone(row['sha256'])
        self.assertIsNone(row['s3_key'])
        self.assertEqual(CODE_DUPLICATE, row['error_code'])
        # Exactly the loser's key was removed, after its transfer completed.
        self.assertEqual(1, len(self.s3.deletes))
        self.assertNotEqual(won['s3_key'], self.s3.deletes[0])

    async def test_a_deleted_document_mid_transfer_is_not_revived(self) -> None:
        document = await self.seed_document()
        task = await self.hold_transfer(document)
        # The document is deleted while the transfer thread is still running. The
        # row change happens on this thread: the SQLite double is thread-bound.
        self.h.raw.execute(
            "UPDATE document SET state = 'deleted', deleted_at = ?, locked_by = NULL,"
            ' lease_token = NULL, locked_at = NULL WHERE id = ?',
            ('2020-01-01T00:00:00+00:00', document['id']),
        )
        self.h.raw.commit()
        error = await self.finish_transfer(task)

        # A deleted document is not found, and never comes back through a late finish.
        self.assertEqual(CODE_NOT_FOUND, error.code)
        row = self.h.document_row(document['id'])
        self.assertEqual('deleted', row['state'])
        self.assertIsNotNone(row['deleted_at'])
        self.assertIsNone(row['sha256'])
        # The abandoned attempt object is the only thing removed.
        self.assertEqual(1, len(self.s3.deletes))

    async def test_a_superseded_attempt_does_not_revive_the_row(self) -> None:
        document = await self.seed_document()
        task = await self.hold_transfer(document)
        # What a later ``retry_upload`` leaves behind: pending, no token, no key.
        self.h.raw.execute(
            "UPDATE document SET state = 'pending_upload', s3_key = NULL, locked_by = NULL,"
            ' lease_token = NULL, locked_at = NULL WHERE id = ?',
            (document['id'],),
        )
        self.h.raw.commit()
        error = await self.finish_transfer(task)

        self.assertEqual(CODE_CONFLICT, error.code)
        row = self.h.document_row(document['id'])
        self.assertEqual('pending_upload', row['state'])
        self.assertIsNone(row['sha256'])
        self.assertIsNone(row['s3_key'])
        self.assertEqual(1, len(self.s3.deletes))

    async def test_a_published_row_referencing_our_key_keeps_the_object(self) -> None:
        document = await self.seed_document()
        task = await self.hold_transfer(document)
        referenced = self.h.document_row(document['id'])['s3_key']
        # The same attempt's key is now owned by a row that is already queued.
        self.h.raw.execute(
            "UPDATE document SET state = 'queued', locked_by = NULL, lease_token = NULL, locked_at = NULL WHERE id = ?",
            (document['id'],),
        )
        self.h.raw.commit()
        error = await self.finish_transfer(task)

        self.assertEqual(CODE_CONFLICT, error.code)
        row = self.h.document_row(document['id'])
        self.assertEqual('queued', row['state'])
        self.assertEqual(referenced, row['s3_key'])
        # A published row still references the object, so it is never deleted.
        self.assertEqual([], self.s3.deletes)


class CancellationTest(_UploadTestCase):
    """A cancellation never abandons the work or removes a possibly-live object."""

    async def test_cancellation_waits_for_the_transfer_before_cleaning_up(self) -> None:
        document = await self.seed_document()
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def _hold(_path: Path) -> None:
            entered.set()
            # Bounded, so a broken service cannot hang the suite.
            self.assertTrue(release.wait(timeout=WAIT_SECONDS))

        self.s3.on_call = _hold
        task = asyncio.ensure_future(self.receive(document['id'], self.stream([b'payload'])))
        self.assertTrue(await asyncio.to_thread(entered.wait, WAIT_SECONDS))
        temp = self.temp_path()
        self.assertTrue(temp.exists())

        task.cancel()
        # The transfer thread is still running, and the task must still be waiting
        # for it rather than having removed the private file or released the lease.
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
        self.assertTrue(temp.exists())
        self.assertEqual('uploading', self.h.document_row(document['id'])['state'])
        self.assertFalse(task.done())

        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertFalse(temp.exists())
        row = self.h.document_row(document['id'])
        self.assertEqual('failed', row['state'])
        # Nothing was published, so the settled attempt object is removed and the
        # journal cleared instead of being left behind.
        self.assertEqual(1, len(self.s3.deletes))
        self.assertIsNone(row['s3_key'])
        self.assertIsNone(row['lease_token'])

    async def test_cancellation_during_receive_removes_the_private_file(self) -> None:
        document = await self.seed_document()
        first_pull = asyncio.Event()
        stream = self.stream([b'partial'], stall=asyncio.Event(), first_pull=first_pull)
        task = asyncio.ensure_future(self.receive(document['id'], stream))
        # Wait until the piece has been received and the stream is blocked waiting
        # for the next one, then cancel exactly as a disconnected client would.
        await asyncio.wait_for(first_pull.wait(), timeout=WAIT_SECONDS)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertFalse(self.temp_path().exists())
        self.assertEqual([], self.s3.calls)
        self.assertEqual([], self.s3.deletes)
        row = self.h.document_row(document['id'])
        self.assertEqual('failed', row['state'])
        self.assertIsNone(row['s3_key'])
        self.assertEqual(CODE_UPLOAD_FAILED, row['error_code'])

    async def test_an_unconfirmed_finishing_commit_keeps_the_object_and_the_journal(self) -> None:
        payload = b'payload'
        document = await self.seed_document()
        # The owner read is commit 1 and the begin journal commit 2; the finishing
        # transaction is commit 3, which physically commits and then loses its
        # confirmation.
        self.h.storage.unknown_commit_at = 3
        error = await self.failed_receive(document['id'], self.stream([payload]))

        self.assertEqual(CODE_PERSISTENCE_FAILED, error.code)
        self.assertEqual('The upload result could not be confirmed.', str(error))
        row = self.h.document_row(document['id'])
        # The commit may have landed, and it did: the row is queued and still
        # references the object, so the object must survive and never be deleted.
        self.assertEqual('queued', row['state'])
        self.assertEqual(hashlib.sha256(payload).hexdigest(), row['sha256'])
        self.assertIsNotNone(row['s3_key'])
        self.assertEqual(1, len(self.s3.calls))
        self.assertEqual([], self.s3.deletes)
        self.assertFalse(self.temp_path().exists())


if __name__ == '__main__':
    unittest.main()
