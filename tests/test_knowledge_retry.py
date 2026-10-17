"""Retrying a failed knowledge-base document.

The service runs over the real ``KnowledgeRepository`` and the in-memory SQLite
double borrowed from ``test_knowledge_repository``, with a storage handle whose
``transaction()`` really commits and rolls back. Object deletion is a
synchronous in-process fake, so no SDK, no network and no configuration are
touched. The route tests ride ``httpx.ASGITransport`` over the real router; no
HTTP server, PostgreSQL or S3 is involved.

What is *not* covered: real PostgreSQL row locks, real S3 deletes, process death
and the import worker - none of which this route reaches. The expiry reaper for
abandoned ``uploading`` rows still does not exist.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import unittest
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from test_knowledge_api import (
    PUBLIC_DOCUMENT_FIELDS,
    SERVER_OWNED_KEYS,
    SERVER_OWNED_VALUES,
    _client,
    _FakeStorage,
    _TxConnection,
)
from test_knowledge_file_api import _app_client, _FakeStore
from test_knowledge_repository import _Harness

from hippo.api.routers import knowledge as knowledge_api
from hippo.knowledge import retry as retry_module
from hippo.knowledge import upload as upload_module
from hippo.knowledge.retry import retry_document
from hippo.knowledge.store import DocumentStoreError
from hippo.knowledge.upload import (
    CODE_CONFLICT,
    CODE_NOT_FOUND,
    CODE_PERSISTENCE_FAILED,
    CODE_UPLOAD_FAILED,
    DocumentUploadError,
)
from hippo.models import User
from hippo.repositories.knowledge import KnowledgeRepository

#: Every blocking wait in this file is bounded by this; nothing asserts on speed.
WAIT_SECONDS = 5.0

PAYLOAD = b'%PDF-1.7\n' + b'body' * 16


class _RetryStorage(_FakeStorage):
    """The SQLite-backed storage double plus one unknown-commit hook.

    ``unknown_commit_at`` is a 1-based commit ordinal that physically commits but
    then reports an error, modelling a lost confirmation on the way back.
    """

    def __init__(self, conn) -> None:
        super().__init__(conn)
        self.unknown_commit_at: int | None = None

    @asynccontextmanager
    async def transaction(self):
        try:
            yield self
        except BaseException:
            self.rollbacks += 1
            await self._conn.rollback()
            raise
        await self._conn.commit()
        self.commits += 1
        if self.unknown_commit_at is not None and self.commits == self.unknown_commit_at:
            raise RuntimeError('commit outcome unknown')


class _RetryHarness(_Harness):
    """Repository harness whose connection the storage handle also commits through."""

    def __init__(self) -> None:
        super().__init__()
        self.connection = _TxConnection(self.raw)
        self.repo = KnowledgeRepository(self.connection)  # type: ignore[arg-type]
        self.storage = _RetryStorage(self.connection)


class _FakeS3(_FakeStore):
    """The file-api store double plus an injectable delete failure and hold hook."""

    def __init__(self, *, delete_error: BaseException | None = None, on_delete=None) -> None:
        super().__init__()
        self.delete_error = delete_error
        self.on_delete = on_delete
        self.delete_is_thread = False

    def delete_document_object(self, key: str) -> None:
        self.delete_is_thread = threading.current_thread() is not threading.main_thread()
        self.deletes.append(key)
        if self.on_delete is not None:
            self.on_delete(key)
        if self.delete_error is not None:
            raise self.delete_error


class _RetryTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.h = _RetryHarness()
        self.fake = _FakeS3()
        self._patch_delete(self.fake)
        self.base = await self.h.repo.create_knowledge_base(1, 'Mine')

    async def asyncTearDown(self) -> None:
        self.h.raw.close()

    def _patch_delete(self, fake: _FakeS3) -> None:
        patched = patch.object(retry_module, 'delete_document_object', new=fake.delete_document_object)
        patched.start()
        self.addCleanup(patched.stop)

    async def _upload_failure(self, *, with_key: bool, owner: int = 1) -> dict:
        """One live ``failed`` upload: with the attempt key journalled, or without."""
        document = await self.h.repo.create_document(
            owner, self.base['id'], filename='a.pdf', mime_type='application/pdf'
        )
        if with_key:
            token = str(uuid4())
            key = f'knowledge/{self.base["id"]}/{document["id"]}/{token}.pdf'
            self.assertTrue(await self.h.repo.begin_upload(owner, document['id'], token, s3_key=key))
            self.assertTrue(
                await self.h.repo.fail_upload(
                    owner,
                    document['id'],
                    token,
                    error_code='document.upload_failed',
                    error_message='The document could not be uploaded.',
                )
            )
        else:
            self.h.raw.execute("UPDATE document SET state = 'failed' WHERE id = ?", (document['id'],))
            self.h.raw.commit()
        return self.h.document_row(document['id'])

    async def _parse_failure(self) -> dict:
        """One ``failed`` import with a complete uploaded source and no lease."""
        document = await self.h.repo.create_document(1, self.base['id'], filename='a.pdf', mime_type='application/pdf')
        token = str(uuid4())
        key = f'knowledge/{self.base["id"]}/{document["id"]}/{token}.pdf'
        self.assertTrue(await self.h.repo.begin_upload(1, document['id'], token, s3_key=key))
        digest = hashlib.sha256(b'payload').hexdigest()
        self.assertEqual(
            'queued',
            await self.h.repo.finish_upload(1, document['id'], token, size_bytes=7, sha256=digest),
        )
        claim = str(uuid4())
        await self.h.repo.claim_import(1, document['id'], 'worker-1', claim)
        await self.h.repo.fail_import(
            1,
            document['id'],
            claim,
            error_code='document.parse_failed',
            error_message='The document could not be parsed.',
        )
        return self.h.document_row(document['id'])


class RetryServiceTest(_RetryTestCase):
    """``retry_document`` over real SQL, with a synchronous fake delete."""

    async def test_an_upload_failure_without_a_key_returns_to_pending(self) -> None:
        row = await self._upload_failure(with_key=False)
        retried = await retry_document(storage=self.h.storage, user_id=1, document_id=row['id'])

        self.assertEqual('pending_upload', retried['state'])
        self.assertEqual('pending_upload', self.h.document_row(row['id'])['state'])
        self.assertIsNone(retried['error_code'])
        self.assertEqual([], self.fake.deletes)

    async def test_an_upload_failure_with_a_key_is_cleaned_then_reset(self) -> None:
        row = await self._upload_failure(with_key=True)
        key = row['s3_key']
        retried = await retry_document(storage=self.h.storage, user_id=1, document_id=row['id'])

        # Exactly the row's own journalled key was deleted, and on a worker thread.
        self.assertEqual([key], self.fake.deletes)
        self.assertTrue(self.fake.delete_is_thread)
        self.assertEqual('pending_upload', retried['state'])
        self.assertIsNone(self.h.document_row(row['id'])['s3_key'])

    async def test_a_failed_delete_keeps_the_journal_and_hides_the_cause(self) -> None:
        secret = 'AccessDenied s3://private/bucket/knowledge/secret.pdf'

        def _fail(_key: str) -> None:
            raise DocumentStoreError(
                'document.delete_failed', 'The stored document could not be deleted.'
            ) from RuntimeError(secret)

        self._patch_delete(_FakeS3(delete_error=None, on_delete=_fail))
        row = await self._upload_failure(with_key=True)

        with self.assertRaises(DocumentUploadError) as caught:
            await retry_document(storage=self.h.storage, user_id=1, document_id=row['id'])

        self.assertEqual(CODE_UPLOAD_FAILED, caught.exception.code)
        self.assertEqual('The document could not be uploaded.', str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn('bucket', str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, DocumentStoreError)
        current = self.h.document_row(row['id'])
        self.assertEqual('failed', current['state'])
        self.assertEqual(row['s3_key'], current['s3_key'])

    async def test_a_parse_failure_requeues_and_never_deletes_the_source(self) -> None:
        row = await self._parse_failure()
        retried = await retry_document(storage=self.h.storage, user_id=1, document_id=row['id'])

        self.assertEqual('queued', retried['state'])
        self.assertEqual([], self.fake.deletes)
        current = self.h.document_row(row['id'])
        self.assertEqual(row['s3_key'], current['s3_key'])
        self.assertEqual(row['sha256'], current['sha256'])
        self.assertEqual(row['size_bytes'], current['size_bytes'])

    async def test_a_parse_failure_without_a_complete_source_is_a_conflict(self) -> None:
        row = await self._parse_failure()
        self.h.raw.execute('UPDATE document SET s3_key = NULL WHERE id = ?', (row['id'],))
        self.h.raw.commit()

        with self.assertRaises(DocumentUploadError) as caught:
            await retry_document(storage=self.h.storage, user_id=1, document_id=row['id'])

        self.assertEqual(CODE_CONFLICT, caught.exception.code)
        self.assertEqual('failed', self.h.document_row(row['id'])['state'])
        self.assertEqual([], self.fake.deletes)

    async def test_foreign_invalid_and_tombstoned_ids_are_not_found(self) -> None:
        row = await self._upload_failure(with_key=True)
        for owner, document_id in ((2, row['id']), (1, str(uuid4())), (1, 'not-a-uuid')):
            with self.assertRaises(DocumentUploadError) as caught:
                await retry_document(storage=self.h.storage, user_id=owner, document_id=document_id)
            self.assertEqual(CODE_NOT_FOUND, caught.exception.code)

        self.assertTrue(await self.h.repo.delete_document(1, row['id']))
        with self.assertRaises(DocumentUploadError) as gone:
            await retry_document(storage=self.h.storage, user_id=1, document_id=row['id'])
        self.assertEqual(CODE_NOT_FOUND, gone.exception.code)
        # A rejected retry never touched the object store.
        self.assertEqual([], self.fake.deletes)

    async def test_a_lost_read_confirmation_is_persistence_failed(self) -> None:
        row = await self._upload_failure(with_key=True)
        # Commit 1 is the owner/state read: it lands but never reports back, so the
        # row is never established and no retry step may run.
        self.h.storage.unknown_commit_at = 1

        with self.assertRaises(DocumentUploadError) as caught:
            await retry_document(storage=self.h.storage, user_id=1, document_id=row['id'])

        self.assertEqual(CODE_PERSISTENCE_FAILED, caught.exception.code)
        self.assertEqual('The retry result could not be confirmed.', str(caught.exception))
        self.assertEqual([], self.fake.deletes)
        self.assertEqual('failed', self.h.document_row(row['id'])['state'])

    async def test_a_failing_owner_read_is_persistence_failed_without_leaking(self) -> None:
        secret = 'psycopg OperationalError: connection to server at "db" failed'

        async def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError(secret)

        row = await self._upload_failure(with_key=True)
        # Production data is a previously committed failure; the failing read's
        # rollback must not also undo the seed.
        self.h.raw.commit()

        patched = patch.object(self.h.storage.knowledge, 'get_document', new=_boom)
        patched.start()
        self.addCleanup(patched.stop)

        with self.assertRaises(DocumentUploadError) as caught:
            await retry_document(storage=self.h.storage, user_id=1, document_id=row['id'])

        self.assertEqual(CODE_PERSISTENCE_FAILED, caught.exception.code)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn('db', str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        # A failed read never reaches the object store.
        self.assertEqual([], self.fake.deletes)
        self.assertEqual('failed', self.h.document_row(row['id'])['state'])

    async def test_every_other_state_is_a_conflict(self) -> None:
        row = await self._upload_failure(with_key=True)
        for state in ('pending_upload', 'uploading', 'queued', 'processing', 'ready', 'unsupported'):
            self.h.raw.execute(
                'UPDATE document SET state = ?, s3_key = NULL, sha256 = NULL, locked_by = NULL,'
                ' lease_token = NULL, locked_at = NULL WHERE id = ?',
                (state, row['id']),
            )
            self.h.raw.commit()
            with self.assertRaises(DocumentUploadError) as caught:
                await retry_document(storage=self.h.storage, user_id=1, document_id=row['id'])
            self.assertEqual(CODE_CONFLICT, caught.exception.code, state)
        self.assertEqual([], self.fake.deletes)

    async def test_an_unknown_commit_keeps_the_possibly_updated_state(self) -> None:
        row = await self._upload_failure(with_key=True)
        # Commit 1 is the owner/state read; commit 2 is the reset CAS, which lands
        # but loses its confirmation.
        self.h.storage.unknown_commit_at = 2

        with self.assertRaises(DocumentUploadError) as caught:
            await retry_document(storage=self.h.storage, user_id=1, document_id=row['id'])

        self.assertEqual(CODE_PERSISTENCE_FAILED, caught.exception.code)
        current = self.h.document_row(row['id'])
        # The commit may have landed, and it did: nothing tries to reverse it.
        self.assertEqual('pending_upload', current['state'])
        self.assertIsNone(current['s3_key'])

    async def test_an_unknown_commit_on_a_parse_retry_is_persistence_failed(self) -> None:
        row = await self._parse_failure()
        self.h.storage.unknown_commit_at = 2

        with self.assertRaises(DocumentUploadError) as caught:
            await retry_document(storage=self.h.storage, user_id=1, document_id=row['id'])

        self.assertEqual(CODE_PERSISTENCE_FAILED, caught.exception.code)
        current = self.h.document_row(row['id'])
        self.assertEqual('queued', current['state'])
        self.assertEqual(row['sha256'], current['sha256'])
        self.assertEqual([], self.fake.deletes)

    async def test_cancellation_waits_for_the_delete_before_resetting(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def _hold(_key: str) -> None:
            entered.set()
            self.assertTrue(release.wait(timeout=WAIT_SECONDS))

        self._patch_delete(_FakeS3(on_delete=_hold))
        row = await self._upload_failure(with_key=True)
        task = asyncio.ensure_future(retry_document(storage=self.h.storage, user_id=1, document_id=row['id']))
        self.assertTrue(await asyncio.to_thread(entered.wait, WAIT_SECONDS))

        task.cancel()
        # The delete thread is still running, so the task must wait rather than
        # reset the row and release the gate early.
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
        self.assertFalse(task.done())
        self.assertEqual('failed', self.h.document_row(row['id'])['state'])

        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        current = self.h.document_row(row['id'])
        self.assertEqual('failed', current['state'])
        self.assertEqual(row['s3_key'], current['s3_key'])

    async def test_concurrent_retries_reset_the_row_only_once(self) -> None:
        row = await self._upload_failure(with_key=True)
        first, second = await asyncio.gather(
            retry_document(storage=self.h.storage, user_id=1, document_id=row['id']),
            retry_document(storage=self.h.storage, user_id=1, document_id=row['id']),
            return_exceptions=True,
        )
        outcomes = [result for result in (first, second) if isinstance(result, dict)]
        conflicts = [result for result in (first, second) if isinstance(result, DocumentUploadError)]
        self.assertEqual(1, len(outcomes))
        self.assertEqual(1, len(conflicts))
        self.assertEqual(CODE_CONFLICT, conflicts[0].code)
        self.assertEqual('pending_upload', self.h.document_row(row['id'])['state'])

    async def test_an_old_key_can_never_reset_a_new_attempt(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def _hold(_key: str) -> None:
            entered.set()
            self.assertTrue(release.wait(timeout=WAIT_SECONDS))

        fake = _FakeS3(on_delete=_hold)
        self._patch_delete(fake)
        row = await self._upload_failure(with_key=True)
        old_key = row['s3_key']
        task = asyncio.ensure_future(retry_document(storage=self.h.storage, user_id=1, document_id=row['id']))
        self.assertTrue(await asyncio.to_thread(entered.wait, WAIT_SECONDS))

        # A newer attempt takes the row (and a new object key) while the delete of
        # the old key is still in flight.
        new_token = str(uuid4())
        new_key = f'knowledge/{self.base["id"]}/{row["id"]}/{new_token}.pdf'
        self.h.raw.execute(
            "UPDATE document SET state = 'uploading', s3_key = ?, lease_token = ?, locked_by = 'upload' WHERE id = ?",
            (new_key, new_token, row['id']),
        )
        self.h.raw.commit()
        release.set()

        with self.assertRaises(DocumentUploadError) as caught:
            await task
        self.assertEqual(CODE_CONFLICT, caught.exception.code)
        current = self.h.document_row(row['id'])
        self.assertEqual('uploading', current['state'])
        self.assertEqual(new_key, current['s3_key'])
        # Only the failed attempt's own key was removed; the new one was never touched.
        self.assertEqual([old_key], fake.deletes)


class RetryApiTest(_RetryTestCase):
    """The route over the real router and the real service."""

    async def test_anonymous_is_rejected(self) -> None:
        row = await self._upload_failure(with_key=False)
        async with _client(self.h.storage) as client:
            response = await client.post(f'/api/document/{row["id"]}/retry')
        self.assertEqual(401, response.status_code)
        self.assertEqual('auth.session_missing', response.json()['code'])
        self.assertEqual('failed', self.h.document_row(row['id'])['state'])

    async def test_foreign_invalid_and_tombstoned_documents_are_404(self) -> None:
        row = await self._upload_failure(with_key=True)
        deleted = await self._upload_failure(with_key=False)
        self.assertTrue(await self.h.repo.delete_document(1, deleted['id']))

        async with _client(self.h.storage, user=User(id=2, username='bob')) as bob:
            foreign = await bob.post(f'/api/document/{row["id"]}/retry')
        self.assertEqual(404, foreign.status_code)
        self.assertEqual('document.not_found', foreign.json()['code'])

        async with _client(self.h.storage, user=User(id=1, username='alice')) as client:
            for document_id in (str(uuid4()), 'not-a-uuid', '0', deleted['id']):
                response = await client.post(f'/api/document/{document_id}/retry')
                self.assertEqual(404, response.status_code, document_id)
                self.assertEqual('document.not_found', response.json()['code'], document_id)
        self.assertEqual([], self.fake.deletes)

    async def test_other_states_answer_409_without_cleanup(self) -> None:
        row = await self._upload_failure(with_key=True)
        async with _client(self.h.storage, user=User(id=1, username='alice')) as client:
            for state in ('pending_upload', 'uploading', 'queued', 'processing', 'ready'):
                self.h.raw.execute(
                    'UPDATE document SET state = ?, s3_key = NULL, sha256 = NULL, locked_by = NULL,'
                    ' lease_token = NULL, locked_at = NULL WHERE id = ?',
                    (state, row['id']),
                )
                self.h.raw.commit()
                response = await client.post(f'/api/document/{row["id"]}/retry')
                self.assertEqual(409, response.status_code, state)
                self.assertEqual('document.conflict', response.json()['code'], state)
        self.assertEqual([], self.fake.deletes)

    async def test_an_upload_retry_answers_the_public_document_then_allows_a_new_put(self) -> None:
        store = _FakeS3()
        for target, name in (
            (upload_module, 'store_document'),
            (upload_module, 'delete_document_object'),
        ):
            patched = patch.object(target, name, new=getattr(store, name))
            patched.start()
            self.addCleanup(patched.stop)
        self._patch_delete(store)

        row = await self._upload_failure(with_key=True)
        old_key = row['s3_key']
        async with _client(self.h.storage, user=User(id=1, username='alice')) as client:
            response = await client.post(f'/api/document/{row["id"]}/retry')
            self.assertEqual(200, response.status_code)
            public = response.json()['document']
            self.assertEqual(PUBLIC_DOCUMENT_FIELDS, tuple(public))
            self.assertEqual('pending_upload', public['state'])
            for secret in (*SERVER_OWNED_KEYS, *SERVER_OWNED_VALUES):
                self.assertNotIn(secret, response.text)
            self.assertNotIn(old_key, response.text)

            # The row is a normal pending upload again, so the client sends the file.
            uploaded = await client.put(
                f'/api/document/{row["id"]}/file',
                content=PAYLOAD,
                headers={'content-type': 'application/pdf'},
            )
        self.assertEqual(202, uploaded.status_code)
        self.assertEqual('queued', uploaded.json()['document']['state'])
        # The failed attempt's key was removed once, and never the new attempt's.
        self.assertEqual([old_key], store.deletes)
        new_key = self.h.document_row(row['id'])['s3_key']
        self.assertNotEqual(old_key, new_key)
        self.assertEqual('queued', self.h.document_row(row['id'])['state'])

    async def test_a_parse_retry_requeues_and_keeps_the_source(self) -> None:
        row = await self._parse_failure()
        async with _client(self.h.storage, user=User(id=1, username='alice')) as client:
            response = await client.post(f'/api/document/{row["id"]}/retry')
        self.assertEqual(200, response.status_code)
        self.assertEqual('queued', response.json()['document']['state'])
        current = self.h.document_row(row['id'])
        self.assertEqual(row['s3_key'], current['s3_key'])
        self.assertEqual(row['sha256'], current['sha256'])
        self.assertEqual([], self.fake.deletes)

    async def test_client_claimed_cleanup_fields_cannot_bypass_the_delete(self) -> None:
        row = await self._upload_failure(with_key=True)
        key = row['s3_key']
        async with _client(self.h.storage, user=User(id=1, username='alice')) as client:
            response = await client.post(
                f'/api/document/{row["id"]}/retry',
                json={'s3_key': 'knowledge/foreign.pdf', 'cleaned_key': 'knowledge/foreign.pdf', 'cleanup': True},
                headers={'x-cleanup': 'true', 'x-s3-key': 'knowledge/foreign.pdf'},
            )
        self.assertEqual(200, response.status_code)
        # Only the row's own journalled key was deleted; the client's claim never decided.
        self.assertEqual([key], self.fake.deletes)
        self.assertEqual('pending_upload', self.h.document_row(row['id'])['state'])

    async def test_two_held_retries_make_the_third_429_and_the_gate_recovers(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        count = 0
        lock = threading.Lock()

        def _hold(_key: str) -> None:
            nonlocal count
            with lock:
                count += 1
                if count == 2:
                    entered.set()
            self.assertTrue(release.wait(timeout=WAIT_SECONDS))

        self._patch_delete(_FakeS3(on_delete=_hold))
        documents = [await self._upload_failure(with_key=True) for _ in range(2)]
        async with _app_client(self.h.storage, user=User(id=1, username='alice')) as (app, client):
            first = asyncio.create_task(client.post(f'/api/document/{documents[0]["id"]}/retry'))
            second = asyncio.create_task(client.post(f'/api/document/{documents[1]["id"]}/retry'))
            self.assertTrue(await asyncio.to_thread(entered.wait, WAIT_SECONDS))

            third = await client.post(f'/api/document/{documents[0]["id"]}/retry')
            self.assertEqual(429, third.status_code)
            self.assertEqual('document.upload_busy', third.json()['code'])

            release.set()
            self.assertEqual(200, (await first).status_code)
            self.assertEqual(200, (await second).status_code)
            self.assertEqual(knowledge_api.MAX_ACTIVE_UPLOADS, app.state.knowledge_upload_semaphore._value)

    async def test_a_failed_retry_releases_its_slot(self) -> None:
        self._patch_delete(_FakeS3(delete_error=DocumentStoreError('document.delete_failed', 'failed')))
        row = await self._upload_failure(with_key=True)
        async with _app_client(self.h.storage, user=User(id=1, username='alice')) as (app, client):
            response = await client.post(f'/api/document/{row["id"]}/retry')
            self.assertEqual(502, response.status_code)
            self.assertEqual('document.upload_failed', response.json()['code'])
            # The gate is released on the error path too.
            self.assertEqual(knowledge_api.MAX_ACTIVE_UPLOADS, app.state.knowledge_upload_semaphore._value)


if __name__ == '__main__':
    unittest.main()
