"""Import worker: claiming one queued knowledge-base document, parsing and publishing.

What this pins down: the queue snapshot is only a snapshot (every candidate is
claimed by CAS under its own owner and a fresh server-side lease token), the
download and the real TXT parser run on a worker thread with no database
connection or storage context open, a business failure is recorded with its own
safe code and keeps the uploaded source, an unknown thread failure collapses to
one fixed code, a deleted or superseded row is never revived or failed late, the
publish and its chunks land in a single transaction, a lost commit answer never
demotes a possibly-``ready`` document, a repeated cancellation waits for the
work it was waiting for before anything is cleaned up, and a working directory
that cannot be created or removed is still a recorded outcome instead of a
lease stuck in ``processing``. While the blocking download and parser run, the
claimed lease is refreshed by one short heartbeat transaction of its own, a
lease that was requeued or replaced stops that heartbeat without cancelling the
thread, a heartbeat database error is logged fixed and retried, and a
cancellation that lands while the heartbeat settles still never publishes. The
expired-lease reaper is exercised here as a real repository call, both directly
and wired into the loop at startup and on its fixed interval.

The object store is a fake boto3 client behind the real ``download_document``,
so the recorded size and digest are verified for real against a tiny payload
before the real TXT parser sees the file. No network, no Poppler, no PostgreSQL
and no process death - the lease SQL is only run against SQLite, real
PostgreSQL concurrency (READ COMMITTED re-evaluation under a heartbeat) and the
``uploading`` journal's crash recovery stay out of scope. The standalone entry
point is inspected, never started.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import tempfile
import threading
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from test_knowledge_repository import _AsyncConnection, _Harness

from hippo.knowledge import store as store_module
from hippo.knowledge import worker as worker_module
from hippo.knowledge.parse import Chunk, DocumentParseError, parse_document
from hippo.knowledge.worker import (
    CODE_IMPORT_CANCELLED,
    CODE_IMPORT_FAILED,
    DocumentImportError,
    recover_expired_imports,
    run_import_once,
    run_import_worker,
)
from hippo.repositories.knowledge import KnowledgeRepository
from hippo.s3 import S3Config

#: Every blocking wait in this file is bounded by this; nothing asserts on speed.
WAIT_SECONDS = 5.0
#: Two paragraphs, so the real TXT parser produces two chunk rows.
PAYLOAD = b'alpha text\n\nbeta text\n'

_CONFIG = S3Config(
    endpoint='http://minio.test',
    bucket='knowledge',
    access_key='key',
    secret_key='secret',
    region='us-east-1',
    prefix='mp/image/',
)


class _TxConnection(_AsyncConnection):
    """The SQLite double plus the commit/rollback ``storage.transaction()`` calls."""

    async def commit(self) -> None:
        self._conn.commit()

    async def rollback(self) -> None:
        self._conn.rollback()


class _FakeStorage:
    """The slice of ``PostgresStorage`` this worker uses, plus counters.

    ``transaction()`` commits and rolls back for real, and both the open storage
    contexts and the open transactions are counted with their peaks, so a run
    that held a connection across the download thread would be caught.
    ``unknown_commit_at`` lets one commit land physically and still report an
    unknown outcome, as a lost confirmation would.
    """

    def __init__(self, conn: _TxConnection) -> None:
        self._conn = conn
        self.knowledge = KnowledgeRepository(conn)  # type: ignore[arg-type]
        self.commits = 0
        self.rollbacks = 0
        self.active = 0
        self.max_active = 0
        self.live_contexts = 0
        self.max_live_contexts = 0
        #: 1-based commit ordinal that physically commits but never reports back.
        self.unknown_commit_at: int | None = None

    def open(self) -> Any:
        """``open_storage()`` stand-in: no pool, no server, just a context."""
        return self._context()

    @asynccontextmanager
    async def _context(self):
        self.live_contexts += 1
        self.max_live_contexts = max(self.max_live_contexts, self.live_contexts)
        try:
            yield self
        finally:
            self.live_contexts -= 1

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


class _WorkerHarness(_Harness):
    """Repository harness whose connection the storage handle also commits through."""

    def __init__(self) -> None:
        super().__init__()
        self.connection = _TxConnection(self.raw)
        self.repo = KnowledgeRepository(self.connection)  # type: ignore[arg-type]
        self.storage = _FakeStorage(self.connection)


class _FakeBody:
    """A bounded stand-in for the SDK body the real download streams from."""

    def __init__(self, payload: bytes, on_read=None) -> None:
        self.payload = payload
        self.on_read = on_read
        self.offset = 0
        self.reads = 0
        self.closed = 0

    def read(self, size: int = -1) -> bytes:
        self.reads += 1
        if self.on_read is not None:
            # Fires once, on whichever worker thread the download is running on.
            hook, self.on_read = self.on_read, None
            hook()
        block = self.payload[self.offset : self.offset + size]
        self.offset += len(block)
        return block

    def close(self) -> None:
        self.closed += 1


class _FakeClient:
    """A fake boto3 client: one small object, and tripwires for every write.

    ``get_object`` records the counters of the owning storage handle, so a test
    can prove that no storage context and no transaction was open when the SDK
    was called. ``on_download`` runs on the worker thread inside the first body
    read, which is what lets a test hold the transfer open and cancel around it.
    """

    def __init__(self, storage: _FakeStorage, payload: bytes = PAYLOAD) -> None:
        self.storage = storage
        self.payload = payload
        self.get_error: Exception | None = None
        self.on_download = None
        self.gets: list[dict] = []
        self.deletes: list[str] = []
        self.caller_is_thread = False
        self.active_at_download: int | None = None
        self.contexts_at_download: int | None = None
        self.body: _FakeBody | None = None

    def get_object(self, **kwargs) -> dict:
        self.gets.append(kwargs)
        self.caller_is_thread = threading.current_thread() is not threading.main_thread()
        self.active_at_download = self.storage.active
        self.contexts_at_download = self.storage.live_contexts
        if self.get_error is not None:
            raise self.get_error
        self.body = _FakeBody(self.payload, on_read=self.on_download)
        return {'Body': self.body, 'ContentLength': len(self.payload)}

    def delete_object(self, **kwargs) -> None:
        self.deletes.append(kwargs['Key'])
        raise AssertionError('the import worker must never delete an uploaded object')

    def delete_objects(self, **_kwargs) -> None:
        raise AssertionError('the import worker must never delete in batches')

    def put_object(self, **_kwargs) -> None:
        raise AssertionError('the import worker must never upload')

    def list_objects_v2(self, **_kwargs) -> None:
        raise AssertionError('the import worker must never list the bucket')


class _WorkerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.h = _WorkerHarness()
        self.client = _FakeClient(self.h.storage)
        self.temp_dirs: list[tempfile.TemporaryDirectory] = []
        self._real_tempdir = tempfile.TemporaryDirectory
        #: Set by ``hold_download`` on this thread and read by the worker thread.
        self.release: threading.Event | None = None
        #: Filled from the worker thread while the private directory is still open.
        self.seen_temp_names: list[Any] = []
        patches = (
            patch.object(worker_module, 'open_storage', self.h.storage.open),
            patch.object(store_module, 'get_s3_client', lambda: (_CONFIG, self.client)),
            patch.object(worker_module.tempfile, 'TemporaryDirectory', self._record_tempdir),
        )
        for patched in patches:
            patched.start()
            self.addCleanup(patched.stop)
        #: How often the real repository refreshed a lease, counted in Python so a
        #: test never reads SQLite from the worker thread. The events are the real
        #: signals a test waits on instead of sleeping: ``beat_happened`` on every
        #: refresh, ``beat_ok`` on a successful one, ``lease_lost`` when the lease is
        #: gone. ``heartbeat_fault`` makes every refresh fail and ``heartbeat_gate``
        #: parks a refresh inside its database call.
        self.beats = 0
        self.heartbeat_fault: BaseException | None = None
        self.heartbeat_gate: asyncio.Event | None = None
        self.beat_happened = asyncio.Event()
        self.beat_ok = asyncio.Event()
        self.lease_lost = asyncio.Event()
        self.contexts_at_beat: int | None = None
        self.active_at_beat: int | None = None
        repo = self.h.storage.knowledge
        real_heartbeat = repo.heartbeat_import

        async def _counted(user_id, document_id, lease_token):
            self.beats += 1
            self.contexts_at_beat = self.h.storage.live_contexts
            self.active_at_beat = self.h.storage.active
            self.beat_happened.set()
            if self.heartbeat_fault is not None:
                raise self.heartbeat_fault
            if self.heartbeat_gate is not None:
                await self.heartbeat_gate.wait()
            result = await real_heartbeat(user_id, document_id, lease_token)
            if result:
                self.beat_ok.set()
            else:
                self.lease_lost.set()
            return result

        repo.heartbeat_import = _counted  # type: ignore[method-assign]

    async def asyncTearDown(self) -> None:
        self.h.raw.close()

    def _record_tempdir(self, **kwargs) -> tempfile.TemporaryDirectory:
        directory = self._real_tempdir(**kwargs)
        self.temp_dirs.append(directory)
        return directory

    def _break_cleanup(self) -> None:
        """Let the worker create its private directory, but fail its own cleanup.

        The real directory is still removed by a test cleanup, so making the
        worker's removal fail never leaves a temporary file behind for the suite.
        """
        real_tempdir = self._real_tempdir

        def _factory(**kwargs) -> tempfile.TemporaryDirectory:
            directory = real_tempdir(**kwargs)
            self.temp_dirs.append(directory)
            real_cleanup = directory.cleanup

            def _refuse() -> None:
                raise OSError('rmtree refused by the test')

            directory.cleanup = _refuse  # type: ignore[method-assign]
            self.addCleanup(real_cleanup)
            return directory

        patcher = patch.object(worker_module.tempfile, 'TemporaryDirectory', _factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def queue_document(
        self,
        *,
        owner: int = 1,
        mime: str = 'text/plain',
        filename: str = 'notes.txt',
        payload: bytes = PAYLOAD,
    ) -> dict:
        """One document taken to a free ``queued`` row through the repository."""
        base = await self.h.repo.create_knowledge_base(owner, f'Base {uuid4().hex[:8]}')
        doc = await self.h.repo.create_document(owner, base['id'], filename=filename, mime_type=mime)
        token = str(uuid4())
        key = f'knowledge/{base["id"]}/{doc["id"]}/{token}.txt'
        self.assertTrue(await self.h.repo.begin_upload(owner, doc['id'], token, s3_key=key))
        self.assertEqual(
            await self.h.repo.finish_upload(
                owner,
                doc['id'],
                token,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
            'queued',
        )
        self.h.raw.commit()
        return {'base': base, 'doc': doc, 'key': key}

    def temp_path(self) -> Path:
        """The one private working directory this run created."""
        self.assertEqual(1, len(self.temp_dirs))
        return Path(self.temp_dirs[0].name)

    def temp_exists(self) -> bool:
        return any(Path(directory.name).exists() for directory in self.temp_dirs)

    def pending_tasks(self) -> set[asyncio.Task]:
        """The worker's leftover tasks; the test's own task is excluded.

        A heartbeat that forgot to settle, or a leaked fire-and-forget task, shows
        up here instead of only as a slow test.
        """
        current = asyncio.current_task()
        return {task for task in asyncio.all_tasks() if task is not current and not task.done()}

    async def settle(self) -> None:
        """Hand the loop a few turns so a finished task really reports as done."""
        for _ in range(3):
            await asyncio.sleep(0)

    def tighten_heartbeat(self) -> None:
        """Shorten the fixed interval so a test never waits a real 30 seconds."""
        patcher = patch.object(worker_module, '_HEARTBEAT_SECONDS', 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def hold_download(self, *, fail_after_release: bool = False) -> asyncio.Task:
        """Start a run whose download thread is parked inside its first read.

        With ``fail_after_release`` the parked read fails once released, so the
        failure path can be exercised against a row somebody else changed.
        """
        entered = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        release = self.release

        def _hold() -> None:
            entered.set()
            # Bounded, so a broken worker cannot hang the suite.
            self.assertTrue(release.wait(timeout=WAIT_SECONDS))
            if fail_after_release:
                raise RuntimeError('connection reset')

        self.client.on_download = _hold
        task = asyncio.ensure_future(run_import_once(worker_id='worker-1'))
        self.assertTrue(await asyncio.to_thread(entered.wait, WAIT_SECONDS))
        return task


class ClaimTest(_WorkerTestCase):
    """Listing is only a snapshot; every candidate still has to be claimed."""

    async def test_an_empty_queue_reports_no_work_and_touches_nothing(self) -> None:
        self.assertFalse(await run_import_once(worker_id='worker-1'))

        self.assertEqual([], self.client.gets)
        self.assertEqual([], self.client.deletes)
        self.assertEqual([], self.temp_dirs)
        self.assertEqual(0, self.h.storage.live_contexts)
        self.assertEqual(1, self.h.storage.commits)

    async def test_a_queued_document_is_claimed_and_parsed(self) -> None:
        queued = await self.queue_document()

        self.assertTrue(await run_import_once(worker_id='worker-1'))

        row = self.h.document_row(queued['doc']['id'])
        self.assertEqual('ready', row['state'])
        self.assertEqual(1, row['attempts'])
        self.assertEqual(queued['key'], row['s3_key'])
        self.assertEqual([{'Bucket': 'knowledge', 'Key': queued['key']}], self.client.gets)

    async def test_a_stale_snapshot_claims_nothing(self) -> None:
        first = await self.queue_document()
        second = await self.queue_document()
        snapshot = await self.h.repo.list_queued_imports()
        self.assertEqual({first['doc']['id'], second['doc']['id']}, {row['id'] for row in snapshot})
        # Another worker claims both rows after our snapshot was taken, so both
        # CAS attempts must miss even though the listing still shows them.
        for row in snapshot:
            self.assertIsNotNone(await self.h.repo.claim_import(row['user_id'], row['id'], 'worker-2', str(uuid4())))
        self.h.raw.commit()

        with patch.object(KnowledgeRepository, 'list_queued_imports', AsyncMock(return_value=snapshot)):
            self.assertFalse(await run_import_once(worker_id='worker-1'))

        for queued in (first, second):
            row = self.h.document_row(queued['doc']['id'])
            self.assertEqual('processing', row['state'])
            self.assertEqual('worker-2', row['locked_by'])
        self.assertEqual([], self.client.gets)
        self.assertEqual([], self.client.deletes)
        self.assertEqual([], self.temp_dirs)

    async def test_the_claim_uses_the_candidate_owner_and_a_fresh_token(self) -> None:
        queued = await self.queue_document()
        captured: list = []

        async def _capture(claim) -> None:
            captured.append(claim)

        with patch.object(worker_module, '_process_claim', _capture):
            self.assertTrue(await run_import_once(worker_id='worker-1'))

        claim = captured[0]
        self.assertEqual(1, claim.user_id)
        self.assertEqual(queued['doc']['id'], claim.document_id)
        self.assertEqual(queued['key'], claim.s3_key)
        self.assertEqual(len(PAYLOAD), claim.size_bytes)
        self.assertEqual(hashlib.sha256(PAYLOAD).hexdigest(), claim.sha256)
        self.assertEqual('text/plain', claim.mime_type)
        # The lease token is the server's own uuid4 and reaches the row as such.
        self.assertEqual(str(UUID(claim.lease_token)), claim.lease_token)
        self.assertNotEqual(queued['key'], claim.lease_token)
        row = self.h.document_row(queued['doc']['id'])
        self.assertEqual('processing', row['state'])
        self.assertEqual(claim.lease_token, row['lease_token'])
        self.assertEqual('worker-1', row['locked_by'])

    async def test_another_owners_queued_document_is_claimed_under_its_own_owner(self) -> None:
        queued = await self.queue_document(owner=2)

        self.assertTrue(await run_import_once(worker_id='worker-1'))

        self.assertEqual(1, len(self.client.gets))
        self.assertEqual('ready', self.h.document_row(queued['doc']['id'])['state'])


class ImportTest(_WorkerTestCase):
    """The real download and the real TXT parser run off-connection, then publish."""

    async def test_a_text_document_is_downloaded_parsed_and_published(self) -> None:
        queued = await self.queue_document()
        # The reference parse runs the same real parser on the same bytes, in a
        # directory of its own (the recording hook must stay unused here).
        with self._real_tempdir() as directory:
            reference = Path(directory) / 'expected.txt'
            reference.write_bytes(PAYLOAD)
            expected = parse_document(reference, 'text/plain')

        started = self.h.storage.commits
        self.assertTrue(await run_import_once(worker_id='worker-1'))

        row = self.h.document_row(queued['doc']['id'])
        self.assertEqual('ready', row['state'])
        self.assertEqual(expected.page_count, row['page_count'])
        self.assertEqual(2, row['version'])
        self.assertEqual(1, row['attempts'])
        self.assertIsNone(row['lease_token'])
        self.assertIsNone(row['locked_by'])
        self.assertIsNone(row['locked_at'])
        self.assertIsNone(row['error_code'])
        self.assertIsNone(row['error_message'])
        # The uploaded source is untouched; this worker never deletes an object.
        self.assertEqual(queued['key'], row['s3_key'])
        self.assertEqual(hashlib.sha256(PAYLOAD).hexdigest(), row['sha256'])
        chunks = await self.h.repo.list_chunks(1, queued['doc']['id'])
        self.assertEqual(
            [(item['page_no'], item['ordinal'], item['text']) for item in chunks],
            [(chunk.page_no, chunk.ordinal, chunk.text) for chunk in expected.chunks],
        )
        # Two short transactions only: the claim and the publish.
        self.assertEqual(started + 2, self.h.storage.commits)
        self.assertEqual(0, self.h.storage.rollbacks)
        self.assertEqual(1, self.h.storage.max_active)
        self.assertEqual(1, self.h.storage.max_live_contexts)
        self.assertEqual([], self.client.deletes)

    async def test_the_download_runs_off_connection_on_a_worker_thread(self) -> None:
        await self.queue_document()

        self.assertTrue(await run_import_once(worker_id='worker-1'))

        self.assertTrue(self.client.caller_is_thread)
        self.assertEqual(0, self.client.active_at_download)
        self.assertEqual(0, self.client.contexts_at_download)
        self.assertEqual(0, self.h.storage.live_contexts)
        self.assertEqual(0, self.h.storage.active)
        self.assertFalse(self.temp_exists())

    async def test_a_source_that_does_not_match_its_record_is_refused(self) -> None:
        queued = await self.queue_document()
        # The object now holds different bytes than the row's recorded digest.
        self.client.payload = b'tampered payload\n'

        self.assertTrue(await run_import_once(worker_id='worker-1'))

        row = self.h.document_row(queued['doc']['id'])
        self.assertEqual('failed', row['state'])
        self.assertEqual('document.content_mismatch', row['error_code'])
        self.assertEqual('The downloaded document does not match its record.', row['error_message'])
        self.assertEqual(queued['key'], row['s3_key'])
        self.assertEqual([], await self.h.repo.list_chunks(1, queued['doc']['id']))
        self.assertEqual([], self.client.deletes)

    async def test_the_private_file_is_fixed_and_never_built_from_the_row(self) -> None:
        queued = await self.queue_document(filename='../../private client name.txt')

        def _remember_contents() -> None:
            # Runs on the worker thread while the private directory is still open.
            self.seen_temp_names.append(sorted(item.name for item in Path(self.temp_dirs[0].name).iterdir()))

        self.client.on_download = _remember_contents

        self.assertTrue(await run_import_once(worker_id='worker-1'))

        # One fixed destination file name, taken from nothing in the row.
        self.assertEqual([['source']], self.seen_temp_names)
        temp = self.temp_path()
        self.assertTrue(temp.name.startswith('hippo-knowledge-import-'))
        # The file inside the private directory is the fixed name, never the key,
        # the stored file name or anything else taken from the row.
        self.assertFalse(self.temp_exists())
        self.assertNotIn('private', str(temp))
        self.assertNotIn('client', str(temp))
        self.assertNotIn(queued['doc']['id'], str(temp))
        self.assertEqual('ready', self.h.document_row(queued['doc']['id'])['state'])


class LateResultTest(_WorkerTestCase):
    """A deleted or superseded row never receives a late result."""

    async def test_a_document_deleted_mid_download_drops_the_late_result(self) -> None:
        queued = await self.queue_document()
        task = await self.hold_download()
        self.assertTrue(self.temp_path().exists())
        self.assertEqual('processing', self.h.document_row(queued['doc']['id'])['state'])

        self.assertTrue(await self.h.repo.delete_document(1, queued['doc']['id']))
        self.h.raw.commit()
        self.release.set()

        self.assertTrue(await task)
        row = self.h.document_row(queued['doc']['id'])
        # The tombstone is not revived, no failure is invented and no chunk lands.
        self.assertEqual('deleted', row['state'])
        self.assertIsNone(row['error_code'])
        self.assertEqual(queued['key'], row['s3_key'])
        self.assertEqual([], await self.h.repo.list_chunks(1, queued['doc']['id']))
        self.assertFalse(self.temp_exists())
        self.assertEqual([], self.client.deletes)

    async def test_a_late_failure_after_a_deletion_does_not_resurrect_the_row(self) -> None:
        queued = await self.queue_document()
        task = await self.hold_download(fail_after_release=True)
        self.assertTrue(await self.h.repo.delete_document(1, queued['doc']['id']))
        self.h.raw.commit()
        self.release.set()

        # The download then fails, so the failure path runs against a row that no
        # longer belongs to this lease: nothing may be written to it.
        self.assertTrue(await task)

        row = self.h.document_row(queued['doc']['id'])
        self.assertEqual('deleted', row['state'])
        self.assertIsNone(row['error_code'])
        self.assertIsNone(row['error_message'])
        self.assertEqual(queued['key'], row['s3_key'])
        self.assertEqual([], self.client.deletes)

    async def test_a_superseded_lease_drops_the_late_result(self) -> None:
        queued = await self.queue_document()
        task = await self.hold_download()
        # A newer worker took the lease while this run was still downloading.
        new_token = str(uuid4())
        self.h.raw.execute(
            "UPDATE document SET lease_token = ?, locked_by = 'worker-2' WHERE id = ?",
            (new_token, queued['doc']['id']),
        )
        self.h.raw.commit()
        self.release.set()

        self.assertTrue(await task)

        row = self.h.document_row(queued['doc']['id'])
        self.assertEqual('processing', row['state'])
        self.assertEqual(new_token, row['lease_token'])
        self.assertEqual('worker-2', row['locked_by'])
        self.assertIsNone(row['error_code'])
        self.assertEqual([], await self.h.repo.list_chunks(1, queued['doc']['id']))
        self.assertEqual([], self.client.deletes)


class FailureTest(_WorkerTestCase):
    """A download or parse failure is recorded safely; the source is kept."""

    async def test_a_download_failure_is_recorded_with_its_own_safe_code(self) -> None:
        queued = await self.queue_document()
        self.client.get_error = RuntimeError('AccessDenied for knowledge/secret/path')

        with self.assertLogs('hippo.knowledge.worker', level=logging.WARNING) as logs:
            self.assertTrue(await run_import_once(worker_id='worker-1'))

        row = self.h.document_row(queued['doc']['id'])
        self.assertEqual('failed', row['state'])
        self.assertEqual('document.download_failed', row['error_code'])
        self.assertEqual('The document could not be downloaded.', row['error_message'])
        self.assertEqual(1, row['attempts'])
        self.assertEqual(1, row['version'])
        # The uploaded source survives for a later retry.
        self.assertEqual(queued['key'], row['s3_key'])
        self.assertEqual(len(PAYLOAD), row['size_bytes'])
        self.assertEqual([], self.client.deletes)
        self.assertTrue(self.client.caller_is_thread)
        # One fixed admin line; the SDK text and the key are not in the log.
        self.assertEqual(1, len(logs.output))
        self.assertIn('document.download_failed', logs.output[0])
        for leak in ('AccessDenied', queued['key']):
            self.assertNotIn(leak, logs.output[0])
        self.assertFalse(self.temp_exists())

    async def test_a_parse_failure_is_recorded_with_its_own_safe_code(self) -> None:
        queued = await self.queue_document()

        def _refuse(_path: Path, _mime: str):
            raise DocumentParseError('document.needs_ocr', 'The PDF has no extractable text layer; OCR is required.')

        with patch.object(worker_module, 'parse_document', _refuse):
            self.assertTrue(await run_import_once(worker_id='worker-1'))

        row = self.h.document_row(queued['doc']['id'])
        self.assertEqual('failed', row['state'])
        self.assertEqual('document.needs_ocr', row['error_code'])
        self.assertEqual('The PDF has no extractable text layer; OCR is required.', row['error_message'])
        self.assertEqual(queued['key'], row['s3_key'])
        self.assertEqual([], await self.h.repo.list_chunks(1, queued['doc']['id']))
        self.assertEqual([], self.client.deletes)
        self.assertFalse(self.temp_exists())

    async def test_an_unknown_thread_failure_uses_one_fixed_code_and_message(self) -> None:
        queued = await self.queue_document()

        def _explode(_path: Path, _mime: str):
            raise RuntimeError('OOM at /private/tmp/source token=secret')

        with (
            patch.object(worker_module, 'parse_document', _explode),
            self.assertLogs('hippo.knowledge.worker', level=logging.WARNING) as logs,
        ):
            self.assertTrue(await run_import_once(worker_id='worker-1'))

        row = self.h.document_row(queued['doc']['id'])
        self.assertEqual('failed', row['state'])
        self.assertEqual(CODE_IMPORT_FAILED, row['error_code'])
        self.assertEqual('The document could not be imported.', row['error_message'])
        # Nothing from the original failure reaches the stored or logged text.
        stored = f'{row["error_code"]} {row["error_message"]}'
        logged = ''.join(logs.output)
        for leak in ('OOM', 'private', 'token', 'secret', 'RuntimeError'):
            self.assertNotIn(leak, stored)
            self.assertNotIn(leak, logged)
        self.assertIn(CODE_IMPORT_FAILED, logged)
        self.assertEqual(queued['key'], row['s3_key'])
        self.assertEqual([], self.client.deletes)
        self.assertFalse(self.temp_exists())


class PublishTest(_WorkerTestCase):
    """The publish is atomic, and a lost answer never demotes a ready row."""

    async def test_an_unconfirmed_commit_keeps_the_possibly_ready_document(self) -> None:
        queued = await self.queue_document()
        # Commit 1 is the claim, commit 2 is the publish: the publish lands and
        # then loses its confirmation on the way back.
        self.h.storage.unknown_commit_at = 2

        with self.assertRaises(DocumentImportError) as caught:
            await run_import_once(worker_id='worker-1')

        error = caught.exception
        self.assertEqual('document.persistence_failed', error.code)
        self.assertEqual('The import result could not be confirmed.', str(error))
        self.assertIsInstance(error.__cause__, RuntimeError)
        row = self.h.document_row(queued['doc']['id'])
        # The commit may have landed, and it did: the row and its chunks stay
        # ready, the source survives, and no failure is written over them.
        self.assertEqual('ready', row['state'])
        self.assertEqual(2, row['version'])
        self.assertIsNone(row['error_code'])
        self.assertEqual(queued['key'], row['s3_key'])
        self.assertEqual(len(PAYLOAD), row['size_bytes'])
        self.assertEqual(2, len(await self.h.repo.list_chunks(1, queued['doc']['id'])))
        self.assertEqual([], self.client.deletes)
        self.assertFalse(self.temp_exists())

    async def test_a_cancellation_after_the_committed_publish_still_leaves_it_ready(self) -> None:
        queued = await self.queue_document()
        real_publish = worker_module._complete_transaction
        committed = asyncio.Event()
        resume = asyncio.Event()

        async def _gated(claim, parsed) -> bool:
            result = await real_publish(claim, parsed)
            committed.set()
            # The publish is already committed when the cancellation arrives.
            await resume.wait()
            return result

        with patch.object(worker_module, '_complete_transaction', _gated):
            task = asyncio.ensure_future(run_import_once(worker_id='worker-1'))
            await asyncio.wait_for(committed.wait(), timeout=WAIT_SECONDS)
            task.cancel()
            with self.assertRaises(asyncio.TimeoutError):
                # The transaction's real outcome is awaited, never abandoned.
                await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
            resume.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        row = self.h.document_row(queued['doc']['id'])
        # The committed ready row is never rolled back into a failure.
        self.assertEqual('ready', row['state'])
        self.assertEqual(2, row['version'])
        self.assertIsNone(row['error_code'])
        self.assertEqual(2, len(await self.h.repo.list_chunks(1, queued['doc']['id'])))
        self.assertEqual([], self.client.deletes)


class CancellationTest(_WorkerTestCase):
    """A repeated cancellation waits for the transfer, then records one failure."""

    async def test_a_repeated_cancel_waits_for_the_thread_and_keeps_the_temp(self) -> None:
        queued = await self.queue_document()
        task = await self.hold_download()
        temp = self.temp_path()
        self.assertTrue(temp.exists())

        # The download thread is still running, so the private file must survive
        # and the task must not settle - however often it is cancelled.
        for _ in range(2):
            task.cancel()
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
            self.assertTrue(temp.exists())
            self.assertFalse(task.done())
        self.assertEqual('processing', self.h.document_row(queued['doc']['id'])['state'])

        self.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

        row = self.h.document_row(queued['doc']['id'])
        self.assertEqual('failed', row['state'])
        self.assertEqual(CODE_IMPORT_CANCELLED, row['error_code'])
        self.assertEqual('The import was cancelled.', row['error_message'])
        self.assertIsNone(row['lease_token'])
        self.assertEqual(queued['key'], row['s3_key'])
        self.assertEqual([], await self.h.repo.list_chunks(1, queued['doc']['id']))
        self.assertEqual([], self.client.deletes)
        self.assertFalse(self.temp_exists())


class TempDirectoryTest(_WorkerTestCase):
    """A private directory that cannot be made or removed never strands the row."""

    async def test_a_directory_that_cannot_be_created_records_a_fixed_failure(self) -> None:
        queued = await self.queue_document()

        def _refuse(**_kwargs):
            raise OSError('No space left on device at /private/tmp')

        with (
            patch.object(worker_module.tempfile, 'TemporaryDirectory', _refuse),
            self.assertLogs('hippo.knowledge.worker', level=logging.WARNING) as logs,
        ):
            self.assertTrue(await run_import_once(worker_id='worker-1'))

        row = self.h.document_row(queued['doc']['id'])
        # The claimed row is finished with a fixed failure, not left processing.
        self.assertEqual('failed', row['state'])
        self.assertEqual(CODE_IMPORT_FAILED, row['error_code'])
        self.assertEqual('The document could not be imported.', row['error_message'])
        self.assertIsNone(row['lease_token'])
        self.assertIsNone(row['locked_by'])
        self.assertEqual(1, row['attempts'])
        self.assertEqual(queued['key'], row['s3_key'])
        self.assertEqual(len(PAYLOAD), row['size_bytes'])
        self.assertEqual([], await self.h.repo.list_chunks(1, queued['doc']['id']))
        # The SDK was never contacted and the source was never touched.
        self.assertEqual([], self.client.gets)
        self.assertEqual([], self.client.deletes)
        self.assertEqual([], self.temp_dirs)
        self.assertEqual(1, len(logs.output))
        self.assertIn(CODE_IMPORT_FAILED, logs.output[0])
        for leak in ('No space left', 'private', 'OSError'):
            self.assertNotIn(leak, logs.output[0])

    async def test_a_cleanup_failure_keeps_the_parsed_result_and_only_warns(self) -> None:
        queued = await self.queue_document()
        self._break_cleanup()

        with self.assertLogs('hippo.knowledge.worker', level=logging.WARNING) as logs:
            self.assertTrue(await run_import_once(worker_id='worker-1'))

        # The parsed document is published whole; the failed removal only leaks a
        # private temp file, which the test cleanup removes afterwards.
        row = self.h.document_row(queued['doc']['id'])
        self.assertEqual('ready', row['state'])
        self.assertEqual(2, row['version'])
        self.assertIsNone(row['error_code'])
        self.assertEqual(2, len(await self.h.repo.list_chunks(1, queued['doc']['id'])))
        self.assertEqual(queued['key'], row['s3_key'])
        self.assertEqual([], self.client.deletes)
        self.assertEqual(1, len(logs.output))
        self.assertIn(worker_module._CLEANUP_FAILED_LOG, logs.output[0])
        for leak in ('rmtree', 'hippo-knowledge-import-', str(self.temp_path())):
            self.assertNotIn(leak, logs.output[0])

    async def test_a_cancelled_loop_still_raises_when_its_cleanup_fails(self) -> None:
        queued = await self.queue_document()
        self._break_cleanup()
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def _hold() -> None:
            entered.set()
            # Bounded, so a broken worker cannot hang the suite.
            self.assertTrue(release.wait(timeout=WAIT_SECONDS))

        self.client.on_download = _hold
        close = AsyncMock()
        with (
            patch.object(worker_module, 'recover_expired_imports', AsyncMock()),
            patch.object(worker_module, 'close_pool', close),
            self.assertLogs('hippo.knowledge.worker', level=logging.WARNING) as logs,
        ):
            task = asyncio.ensure_future(run_import_worker(worker_id='worker-1'))
            self.assertTrue(await asyncio.to_thread(entered.wait, WAIT_SECONDS))
            task.cancel()
            release.set()
            # The cleanup failure never replaces the cancellation, so the loop
            # really leaves instead of being retried as a plain exception.
            with self.assertRaises(asyncio.CancelledError):
                # Bounded well below the loop's backoff, so a loop that
                # swallowed the cancellation fails here instead of sleeping on.
                await asyncio.wait_for(task, timeout=WAIT_SECONDS / 5)

        close.assert_awaited_once_with()
        row = self.h.document_row(queued['doc']['id'])
        self.assertEqual('failed', row['state'])
        self.assertEqual(CODE_IMPORT_CANCELLED, row['error_code'])
        self.assertIsNone(row['lease_token'])
        self.assertEqual(queued['key'], row['s3_key'])
        self.assertEqual([], self.client.deletes)
        self.assertEqual(1, len(logs.output))
        self.assertIn(worker_module._CLEANUP_FAILED_LOG, logs.output[0])
        for leak in ('rmtree', 'hippo-knowledge-import-'):
            self.assertNotIn(leak, logs.output[0])


class HeartbeatLeaseTest(_WorkerTestCase):
    """A claimed lease is renewed by short beats that never hold the long IO.

    The blocking download is parked on a real thread event, so every wait here is
    driven by a real signal - the heartbeat callback or the lease-lost answer -
    instead of sleeping and guessing that the job is still running.
    """

    async def beats_reached(self, count: int) -> None:
        """Wait until ``count`` heartbeats were attempted, reading only a counter."""
        while self.beats < count:
            await asyncio.sleep(0)

    async def test_a_blocked_download_is_kept_alive_by_short_heartbeats(self) -> None:
        self.tighten_heartbeat()
        queued = await self.queue_document()

        task = await self.hold_download()
        await asyncio.wait_for(self.beat_ok.wait(), timeout=WAIT_SECONDS)
        # The refresh runs inside a storage context and a transaction of its own.
        self.assertEqual(1, self.contexts_at_beat)
        self.assertEqual(1, self.active_at_beat)
        # The download thread itself saw no transaction and no storage context.
        self.assertEqual(0, self.client.active_at_download)
        self.assertEqual(0, self.client.contexts_at_download)
        self.assertGreaterEqual(self.beats, 1)

        self.release.set()
        self.assertTrue(await task)

        row = self.h.document_row(queued['doc']['id'])
        self.assertEqual('ready', row['state'])
        self.assertIsNone(row['lease_token'])
        self.assertEqual(2, len(await self.h.repo.list_chunks(1, queued['doc']['id'])))
        self.assertEqual([], self.client.deletes)
        self.assertFalse(self.temp_exists())
        self.assertEqual(0, self.h.storage.live_contexts)
        self.assertEqual(0, self.h.storage.active)
        self.assertEqual(set(), self.pending_tasks())

    async def test_a_replaced_lease_stops_the_heartbeat_but_not_the_thread(self) -> None:
        self.tighten_heartbeat()
        queued = await self.queue_document()
        task = await self.hold_download()
        await asyncio.wait_for(self.beat_ok.wait(), timeout=WAIT_SECONDS)

        # Another holder moved the lease on while this run was still downloading.
        new_token = str(uuid4())
        self.h.raw.execute(
            "UPDATE document SET lease_token = ?, locked_by = 'worker-2' WHERE id = ?",
            (new_token, queued['doc']['id']),
        )
        self.h.raw.commit()
        await asyncio.wait_for(self.lease_lost.wait(), timeout=WAIT_SECONDS)
        lost_at = self.beats

        # Renewal stopped, but the synchronous transfer is not cancelled: it ends
        # on its own and the late result is dropped by the publish CAS.
        self.release.set()
        self.assertTrue(await task)

        row = self.h.document_row(queued['doc']['id'])
        self.assertEqual('processing', row['state'])
        self.assertEqual(new_token, row['lease_token'])
        self.assertEqual('worker-2', row['locked_by'])
        self.assertIsNone(row['error_code'])
        self.assertEqual([], await self.h.repo.list_chunks(1, queued['doc']['id']))
        self.assertTrue(self.client.caller_is_thread)
        self.assertEqual(lost_at, self.beats)
        self.assertFalse(self.temp_exists())
        self.assertEqual(set(), self.pending_tasks())

    async def test_a_heartbeat_database_error_is_logged_fixed_and_retried(self) -> None:
        self.tighten_heartbeat()
        queued = await self.queue_document()
        self.heartbeat_fault = RuntimeError('SQL text at /private/tmp token=secret')

        with self.assertLogs('hippo.knowledge.worker', level=logging.WARNING) as logs:
            task = await self.hold_download()
            # A database error is retried on the next normal tick, not in a loop.
            await asyncio.wait_for(self.beats_reached(3), timeout=WAIT_SECONDS)
            self.release.set()
            self.assertTrue(await task)

        row = self.h.document_row(queued['doc']['id'])
        self.assertEqual('ready', row['state'])
        self.assertGreaterEqual(self.beats, 3)
        self.assertGreaterEqual(len(logs.output), 2)
        for line in logs.output:
            self.assertIn(worker_module._HEARTBEAT_FAILED_LOG, line)
            for leak in ('SQL text', 'private', 'token', 'secret'):
                self.assertNotIn(leak, line)

    async def test_a_cancel_while_the_heartbeat_settles_still_never_publishes(self) -> None:
        self.tighten_heartbeat()
        queued = await self.queue_document()
        # Park the heartbeat inside its own small database call, after the first
        # beat started but before it answered.
        self.heartbeat_gate = asyncio.Event()
        task = await self.hold_download()
        await asyncio.wait_for(self.beat_happened.wait(), timeout=WAIT_SECONDS)

        self.release.set()
        task.cancel()
        with self.assertRaises(asyncio.TimeoutError):
            # The heartbeat's real outcome is awaited, never abandoned.
            await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
        self.assertFalse(task.done())
        # No ready result was published while the heartbeat was still settling.
        self.assertEqual('processing', self.h.document_row(queued['doc']['id'])['state'])
        self.assertTrue(self.temp_path().exists())

        self.heartbeat_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

        row = self.h.document_row(queued['doc']['id'])
        self.assertEqual('failed', row['state'])
        self.assertEqual(CODE_IMPORT_CANCELLED, row['error_code'])
        self.assertIsNone(row['lease_token'])
        self.assertEqual([], await self.h.repo.list_chunks(1, queued['doc']['id']))
        self.assertFalse(self.temp_exists())
        self.assertEqual(set(), self.pending_tasks())


class RecoverExpiredImportsTest(_WorkerTestCase):
    """The recovery pass runs the real repository against SQLite for real.

    The loop tests stub this entry point to isolate the backoff branches; these
    drive ``requeue_expired_imports`` itself with the fixed internal cutoff.
    """

    async def stale_processing(self, *, locked_at: str = '2020-01-01T00:00:00+00:00') -> dict:
        """One ``queued`` document claimed into a ``processing`` lease, aged by hand."""
        queued = await self.queue_document()
        token = str(uuid4())
        self.assertIsNotNone(await self.h.repo.claim_import(1, queued['doc']['id'], 'worker-2', token))
        self.h.raw.execute('UPDATE document SET locked_at = ? WHERE id = ?', (locked_at, queued['doc']['id']))
        self.h.raw.commit()
        return {**queued, 'token': token}

    async def test_a_stale_lease_is_requeued_and_its_old_token_dies(self) -> None:
        stale = await self.stale_processing()
        self.h.seed_chunk(stale['doc']['id'], 1, 0, 'kept text')

        with self.assertLogs('hippo.knowledge.worker', level=logging.INFO) as logs:
            self.assertEqual(1, await recover_expired_imports())

        row = self.h.document_row(stale['doc']['id'])
        self.assertEqual('queued', row['state'])
        self.assertIsNone(row['locked_by'])
        self.assertIsNone(row['lease_token'])
        self.assertIsNone(row['locked_at'])
        # The uploaded source, its digest and the derived chunks are left alone.
        self.assertEqual(stale['key'], row['s3_key'])
        self.assertEqual(len(PAYLOAD), row['size_bytes'])
        self.assertEqual([item['text'] for item in await self.h.repo.list_chunks(1, stale['doc']['id'])], ['kept text'])
        # Nothing is read, deleted, listed or uploaded from the object store.
        self.assertEqual([], self.client.gets)
        self.assertEqual([], self.client.deletes)
        self.assertEqual(0, self.h.storage.live_contexts)
        # One fixed line carrying the real count and nothing from the row or key.
        self.assertEqual(1, len(logs.output))
        self.assertIn('1', logs.output[0])
        for leak in (stale['doc']['id'], stale['key'], stale['token'], 'kept'):
            self.assertNotIn(leak, logs.output[0])

        # The reclaimed lease is dead: a late heartbeat, publish or failure is refused.
        chunks = [Chunk(1, 0, 'late')]
        self.assertFalse(await self.h.repo.heartbeat_import(1, stale['doc']['id'], stale['token']))
        self.assertFalse(
            await self.h.repo.complete_import(1, stale['doc']['id'], stale['token'], page_count=1, chunks=chunks)
        )
        self.assertFalse(
            await self.h.repo.fail_import(1, stale['doc']['id'], stale['token'], error_code='c', error_message='m')
        )
        self.assertEqual('queued', self.h.document_row(stale['doc']['id'])['state'])

    async def test_a_renewed_lease_is_left_for_its_own_worker(self) -> None:
        fresh = await self.stale_processing(locked_at='2999-01-01T00:00:00+00:00')

        self.assertEqual(0, await recover_expired_imports())

        row = self.h.document_row(fresh['doc']['id'])
        self.assertEqual('processing', row['state'])
        self.assertEqual(fresh['token'], row['lease_token'])
        self.assertEqual('worker-2', row['locked_by'])


class LoopTest(_WorkerTestCase):
    """The standalone loop backs off and closes the pool, without logging secrets."""

    async def test_the_loop_backs_off_on_idle_and_failure_paths(self) -> None:
        outcomes: list = [False, RuntimeError('raw SDK text /private/tmp'), False, asyncio.CancelledError()]
        calls = 0

        async def _once(*, worker_id: str) -> bool:
            nonlocal calls
            result = outcomes[calls]
            calls += 1
            if isinstance(result, BaseException):
                raise result
            return result

        sleep = AsyncMock()
        close = AsyncMock()
        recover = AsyncMock()
        with (
            patch.object(worker_module, 'run_import_once', _once),
            patch.object(worker_module, 'recover_expired_imports', recover),
            patch.object(worker_module.asyncio, 'sleep', sleep),
            patch.object(worker_module, 'close_pool', close),
            self.assertLogs('hippo.knowledge.worker', level=logging.WARNING) as logs,
            self.assertRaises(asyncio.CancelledError),
        ):
            await run_import_worker(worker_id='worker-1')

        self.assertEqual(4, calls)
        self.assertEqual(3, sleep.await_count)
        sleep.assert_awaited_with(worker_module._BACKOFF_SECONDS)
        # Maintenance is stubbed here so only the backoff branches are exercised.
        recover.assert_awaited_once_with()
        close.assert_awaited_once_with()
        # The cancellation left the loop instead of being logged and retried.
        self.assertEqual(1, len(logs.output))
        self.assertIn('knowledge import cycle failed', logs.output[0])
        for leak in ('raw SDK text', 'private', 'worker-1'):
            self.assertNotIn(leak, logs.output[0])

    async def test_the_loop_recovers_at_startup_and_then_at_most_once_per_interval(self) -> None:
        outcomes: list = [False, False, asyncio.CancelledError()]
        calls = 0

        async def _once(*, worker_id: str) -> bool:
            nonlocal calls
            result = outcomes[calls]
            calls += 1
            if isinstance(result, BaseException):
                raise result
            return result

        recover = AsyncMock()
        with (
            patch.object(worker_module, 'run_import_once', _once),
            patch.object(worker_module, 'recover_expired_imports', recover),
            # Startup (0.0), an immediate idle cycle inside the interval, then past it.
            patch.object(worker_module, 'monotonic', side_effect=[0.0, 0.0, 70.0]),
            patch.object(worker_module.asyncio, 'sleep', AsyncMock()),
            patch.object(worker_module, 'close_pool', AsyncMock()),
            self.assertRaises(asyncio.CancelledError),
        ):
            await run_import_worker(worker_id='worker-1')

        # Startup plus the interval expiry: an idle loop still maintains the queue,
        # but a tight cycle inside the interval does not.
        self.assertEqual(2, recover.await_count)

    def test_the_entry_point_is_one_keyword_only_argument(self) -> None:
        signature = inspect.signature(run_import_once)
        self.assertEqual(['worker_id'], list(signature.parameters))
        self.assertEqual(inspect.Parameter.KEYWORD_ONLY, signature.parameters['worker_id'].kind)
        self.assertTrue(callable(worker_module.main))

    def test_main_installs_the_configured_logger_and_never_runs_the_worker(self) -> None:
        """The entry point wires the shared logger, without reading config or starting."""
        started: list = []

        def _capture(coroutine) -> None:
            started.append(coroutine)
            # Never awaited here: close it so the suite stays warning-free.
            coroutine.close()

        with (
            patch.object(worker_module, 'setup_logger') as setup,
            patch.object(worker_module.asyncio, 'run', _capture),
            self.assertLogs('hippo.knowledge.worker', level=logging.INFO) as logs,
        ):
            worker_module.main()

        # The service name is what routes this process to its own log file.
        setup.assert_called_once_with(worker_module.__name__, service='hippo-knowledge-worker')
        self.assertEqual(1, len(started))
        self.assertTrue(inspect.iscoroutine(started[0]))
        self.assertEqual('run_import_worker', started[0].cr_code.co_name)
        self.assertIn('knowledge import worker', logs.output[0])

    async def test_the_worker_id_is_ascii_and_unique_per_process(self) -> None:
        first = worker_module._new_worker_id()
        second = worker_module._new_worker_id()

        self.assertTrue(first.isascii())
        self.assertEqual(worker_module._WORKER_ID_PREFIX, first.rsplit('-', 1)[0])
        self.assertNotEqual(first, second)


if __name__ == '__main__':
    unittest.main()
