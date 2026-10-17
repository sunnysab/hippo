"""HTTP contract of the raw knowledge-base upload route.

The route runs over the real router and the same SQLite-backed ``_HttpHarness``
``test_knowledge_api`` uses. Requests go through ``httpx.ASGITransport`` for the
ordinary flows, and through a small direct ASGI driver for the cases that must
prove the adapter never pulled a body piece: httpx owns its own header and body
normalisation, which is exactly what those assertions cannot afford.

For the gate tests the upload service is replaced by an async fake, so the
owner/state precheck, the size/type/encoding headers, the per-app concurrency
gate and cancellation all settle before or around that one call.
``RealUploadServiceTest`` then runs the *real* service over the SQLite double
with a synchronous fake object store, so one request genuinely streams, hashes
and journals the bytes.

No server lifespan, no PostgreSQL, no S3, no network. Every blocking wait is
bounded by ``WAIT_SECONDS``, and no test touches SQLite from another thread.

What is *not* covered: real HTTP framing, real PostgreSQL row locks, real object
storage and the import worker - none of which this adapter reaches.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi import FastAPI
from test_knowledge_api import (
    PUBLIC_DOCUMENT_FIELDS,
    SERVER_OWNED_KEYS,
    SERVER_OWNED_VALUES,
    _HttpHarness,
)

from hippo.api.deps import current_user, get_storage
from hippo.api.errors import install_exception_handlers
from hippo.api.routers import knowledge as knowledge_api
from hippo.knowledge import upload as upload_module
from hippo.knowledge.store import StoredDocument, build_document_key
from hippo.knowledge.upload import (
    CODE_CONFLICT,
    CODE_DUPLICATE,
    CODE_EMPTY,
    CODE_NOT_FOUND,
    CODE_PERSISTENCE_FAILED,
    CODE_TOO_LARGE,
    CODE_UPLOAD_FAILED,
    CODE_UPLOAD_TIMEOUT,
    MAX_FILE_BYTES,
    DocumentUploadError,
)
from hippo.models import User

#: Every blocking wait in this file is bounded by this; nothing asserts on speed.
WAIT_SECONDS = 5.0

#: Small payloads only; the 64 MiB limit is checked from its header, never by
#: allocating the body.
PAYLOAD = b'%PDF-1.7\n' + b'body' * 512


async def _wait_until(predicate, *, timeout: float = WAIT_SECONDS) -> None:
    """Poll a predicate with a hard deadline, so a broken route cannot hang the suite."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError('condition was not met in time')
        await asyncio.sleep(0.01)


def _build_app(storage, *, user: User | None) -> FastAPI:
    """The router under test, with storage - and optionally the user - stubbed."""
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(knowledge_api.router, prefix='/api')

    async def _fake_storage():
        return storage

    app.dependency_overrides[get_storage] = _fake_storage
    if user is not None:
        app.dependency_overrides[current_user] = lambda: user
    return app


@asynccontextmanager
async def _app_client(storage, *, user: User | None = None):
    """An app instance plus its ASGI client; the app's ``state`` is reachable.

    A fresh app per call means a fresh upload semaphore, which the "two apps do
    not share a gate" test relies on.
    """
    app = _build_app(storage, user=user)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url='http://hippo.test',
    ) as client:
        yield app, client


class _AsgiResponse:
    """The status and body collected from one direct ASGI call."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.status_code = 500
        self.body = b''
        for message in messages:
            if message['type'] == 'http.response.start':
                self.status_code = message['status']
            elif message['type'] == 'http.response.body':
                self.body += message.get('body', b'')

    @property
    def text(self) -> str:
        return self.body.decode()

    def json(self) -> Any:
        return json.loads(self.body)


class _AsgiCall:
    """One direct ASGI request the test fully controls.

    The headers are sent verbatim (httpx would normalise them) and ``receive``
    is never called unless the app itself reads the body, so a route that
    over-reads on a rejected request is caught. The final empty ``http.request``
    message is the ASGI "no more body" marker, as a chunked sender would send.
    """

    def __init__(
        self,
        app: FastAPI,
        document_id: str,
        *,
        headers: dict[str, str] | None = None,
        pieces: list[bytes] | None = None,
    ) -> None:
        self.app = app
        self.path = f'/api/document/{document_id}/file'
        self.headers = list((headers or {}).items())
        self.pieces = list(pieces if pieces is not None else [b'payload'])
        self.receive_calls = 0
        self.body_pieces = 0

    async def __call__(self) -> _AsgiResponse:
        messages: list[dict[str, Any]] = []
        remaining = list(self.pieces)

        async def receive() -> dict[str, Any]:
            self.receive_calls += 1
            if remaining:
                self.body_pieces += 1
                return {'type': 'http.request', 'body': remaining.pop(0), 'more_body': True}
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        scope = {
            'type': 'http',
            'asgi': {'version': '3.0', 'spec_version': '2.3'},
            'http_version': '1.1',
            'method': 'PUT',
            'scheme': 'http',
            'path': self.path,
            'raw_path': self.path.encode(),
            'query_string': b'',
            'root_path': '',
            'headers': [(name.lower().encode(), value.encode()) for name, value in self.headers],
            'client': ('127.0.0.1', 51234),
            'server': ('hippo.test', 80),
            'app': self.app,
            'state': {},
        }
        await self.app(scope, receive, send)
        return _AsgiResponse(messages)


class _FakeService:
    """Async stand-in for ``receive_document_file``.

    Records the exact keyword arguments the adapter passed, so a client token,
    key, digest or file name reaching the service would fail the suite. When
    ``release`` is set the call holds, which is what lets the concurrency tests
    occupy a slot.
    """

    def __init__(
        self,
        *,
        row: dict[str, Any] | None = None,
        error: BaseException | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.row = row
        self.error = error
        self.release = release
        self.entered = asyncio.Event()
        self.kwargs: list[set[str]] = []
        self.started: list[str] = []
        self.consumed = 0
        self.payload = bytearray()

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs.append(set(kwargs))
        self.started.append(str(kwargs['document_id']))
        if self.release is not None:
            self.entered.set()
            await asyncio.wait_for(self.release.wait(), WAIT_SECONDS)
        if self.error is not None:
            raise self.error
        async for piece in kwargs['chunks']:
            self.consumed += len(piece)
            self.payload += piece
        return self.row if self.row is not None else _internal_row(str(kwargs['document_id']))


def _internal_row(document_id: str, *, state: str = 'queued') -> dict[str, Any]:
    """A storage row carrying the private columns an adapter must never render."""
    return {
        'id': document_id,
        'knowledge_base_id': str(uuid4()),
        'filename': 'a.pdf',
        'mime_type': 'application/pdf',
        'size_bytes': 7,
        'state': state,
        'attempts': 0,
        'error_code': None,
        'error_message': None,
        'page_count': None,
        'version': 1,
        'created_at': None,
        'updated_at': None,
        's3_key': SERVER_OWNED_VALUES[0],
        'sha256': SERVER_OWNED_VALUES[1],
        'locked_by': SERVER_OWNED_VALUES[2],
        'user_id': 1,
    }


class _FileApiTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.h = _HttpHarness()
        self.alice = User(id=1, username='alice')
        self.bob = User(id=2, username='bob')

    async def _seed_document(self, *, owner: int = 1, mime: str = 'application/pdf') -> dict:
        base = await self.h.repo.create_knowledge_base(owner, 'Mine')
        return await self.h.repo.create_document(owner, base['id'], filename='a.pdf', mime_type=mime)

    def _patch_service(self, service: _FakeService) -> None:
        patched = patch.object(knowledge_api, 'receive_document_file', new=service)
        patched.start()
        self.addCleanup(patched.stop)


class AuthorizationTest(_FileApiTestCase):
    """Auth, ownership and document state settle before any body byte is read."""

    async def test_anonymous_is_rejected_before_the_body(self) -> None:
        document = await self._seed_document()
        service = _FakeService()
        self._patch_service(service)
        call = _AsgiCall(_build_app(self.h.storage, user=None), document['id'])
        response = await call()
        self.assertEqual(401, response.status_code)
        self.assertEqual('auth.session_missing', response.json()['code'])
        self.assertEqual(0, call.receive_calls)
        self.assertEqual([], service.started)

    async def test_foreign_and_unknown_documents_are_404_without_the_body(self) -> None:
        document = await self._seed_document()
        service = _FakeService()
        self._patch_service(service)

        foreign = _AsgiCall(_build_app(self.h.storage, user=self.bob), document['id'])
        foreign_response = await foreign()
        self.assertEqual(404, foreign_response.status_code)
        self.assertEqual('document.not_found', foreign_response.json()['code'])
        self.assertEqual(0, foreign.receive_calls)

        unknown_ids = (str(uuid4()), 'not-a-uuid', '0')
        for document_id in unknown_ids:
            call = _AsgiCall(_build_app(self.h.storage, user=self.alice), document_id)
            response = await call()
            self.assertEqual(404, response.status_code, document_id)
            self.assertEqual('document.not_found', response.json()['code'], document_id)
            self.assertEqual(0, call.receive_calls, document_id)
        self.assertEqual([], service.started)

    async def test_deleted_document_is_404_and_other_states_conflict(self) -> None:
        document = await self._seed_document()
        service = _FakeService()
        self._patch_service(service)
        app = _build_app(self.h.storage, user=self.alice)
        for state in ('uploading', 'queued', 'processing', 'ready', 'failed'):
            self.h.raw.execute('UPDATE document SET state = ? WHERE id = ?', (state, document['id']))
            self.h.raw.commit()
            call = _AsgiCall(app, document['id'])
            response = await call()
            self.assertEqual(409, response.status_code, state)
            self.assertEqual('document.conflict', response.json()['code'], state)
            self.assertEqual(0, call.receive_calls, state)

        self.h.raw.execute(
            'UPDATE document SET state = ?, deleted_at = ? WHERE id = ?',
            ('deleted', '2020-01-01T00:00:00+00:00', document['id']),
        )
        self.h.raw.commit()
        deleted = _AsgiCall(app, document['id'])
        response = await deleted()
        self.assertEqual(404, response.status_code)
        self.assertEqual(0, deleted.receive_calls)
        self.assertEqual([], service.started)


class HeaderGateTest(_FileApiTestCase):
    """Size, type and encoding headers are checked without touching the body."""

    async def _reject(self, headers: dict[str, str], *, mime: str = 'application/pdf') -> _AsgiResponse:
        document = await self._seed_document(mime=mime)
        service = _FakeService()
        self._patch_service(service)
        call = _AsgiCall(
            _build_app(self.h.storage, user=self.alice),
            document['id'],
            headers=headers,
        )
        response = await call()
        self.assertEqual(0, call.receive_calls)
        self.assertEqual([], service.started)
        return response

    async def test_content_length_must_be_a_plain_non_negative_decimal(self) -> None:
        for declared in ('abc', '-1', '+1', '1.0', ' 1', '007x'):
            response = await self._reject({'content-length': declared})
            self.assertEqual(400, response.status_code, declared)
            self.assertEqual('document.invalid_content_length', response.json()['code'], declared)

    async def test_zero_content_length_is_empty_and_oversize_is_413(self) -> None:
        empty = await self._reject({'content-length': '0'})
        self.assertEqual(400, empty.status_code)
        self.assertEqual('document.empty', empty.json()['code'])

        oversized = await self._reject({'content-length': str(MAX_FILE_BYTES + 1)})
        self.assertEqual(413, oversized.status_code)
        self.assertEqual('document.too_large', oversized.json()['code'])

    async def test_content_type_must_be_raw_or_the_document_type(self) -> None:
        for content_type in ('multipart/form-data; boundary=x', 'application/json', 'application/xml'):
            response = await self._reject({'content-type': content_type})
            self.assertEqual(415, response.status_code, content_type)
            self.assertEqual('document.unsupported_mime', response.json()['code'], content_type)

        # The document row is PDF, so a plain-text declaration is a mismatch.
        response = await self._reject({'content-type': 'text/plain'})
        self.assertEqual(415, response.status_code)
        self.assertEqual('document.unsupported_mime', response.json()['code'])

    async def test_content_encoding_other_than_identity_is_refused(self) -> None:
        response = await self._reject({'content-encoding': 'gzip'})
        self.assertEqual(415, response.status_code)
        self.assertEqual('document.unsupported_encoding', response.json()['code'])

    async def test_a_small_declared_length_does_not_cap_the_real_body(self) -> None:
        document = await self._seed_document()
        service = _FakeService()
        self._patch_service(service)
        call = _AsgiCall(
            _build_app(self.h.storage, user=self.alice),
            document['id'],
            headers={'content-length': '3'},
            pieces=[b'12345', b'67890'],
        )
        response = await call()
        self.assertEqual(202, response.status_code)
        # The service counted the bytes it really received, not the header claim.
        self.assertEqual(10, service.consumed)
        self.assertEqual(b'1234567890', bytes(service.payload))


class UploadResponseTest(_FileApiTestCase):
    """The success body is the public projection, never the internal row."""

    async def test_a_chunked_body_reaches_the_service_and_answers_202(self) -> None:
        document = await self._seed_document()
        service = _FakeService()
        self._patch_service(service)
        for headers in (
            {},
            {'content-type': 'application/octet-stream'},
            {'content-type': 'application/pdf'},
            {'content-type': 'Application/PDF; charset=binary'},
            {'content-encoding': 'identity'},
        ):
            call = _AsgiCall(
                _build_app(self.h.storage, user=self.alice),
                document['id'],
                headers=headers,
                pieces=[b'pay', b'load'],
            )
            response = await call()
            self.assertEqual(202, response.status_code, headers)
            self.assertEqual(2, call.body_pieces, headers)
        self.assertEqual(5, len(service.started))
        # Each body was streamed to the service in the two pieces the caller sent.
        self.assertEqual(5 * (len(b'pay') + len(b'load')), service.consumed)

    async def test_a_successful_upload_answers_202_with_the_public_document(self) -> None:
        document = await self._seed_document()
        service = _FakeService()
        self._patch_service(service)
        async with _app_client(self.h.storage, user=self.alice) as (_app, client):
            response = await client.put(
                f'/api/document/{document["id"]}/file',
                content=PAYLOAD,
                headers={'content-type': 'application/pdf'},
            )
        self.assertEqual(202, response.status_code)
        self.assertEqual(['document'], list(response.json()))
        public = response.json()['document']
        self.assertEqual(PUBLIC_DOCUMENT_FIELDS, tuple(public))
        self.assertEqual(document['id'], public['id'])
        for key in (*SERVER_OWNED_KEYS, *SERVER_OWNED_VALUES):
            self.assertNotIn(key, response.text)
        # The adapter asked the service for the stream only: no client token, key,
        # digest or file name is ever an argument.
        self.assertEqual({'storage', 'user_id', 'document_id', 'chunks'}, service.kwargs[0])

    async def test_every_service_error_code_maps_to_its_status(self) -> None:
        expected = {
            CODE_NOT_FOUND: 404,
            CODE_CONFLICT: 409,
            CODE_DUPLICATE: 409,
            CODE_TOO_LARGE: 413,
            CODE_EMPTY: 400,
            CODE_UPLOAD_TIMEOUT: 408,
            CODE_UPLOAD_FAILED: 502,
            CODE_PERSISTENCE_FAILED: 503,
        }
        document = await self._seed_document()
        service = _FakeService()
        self._patch_service(service)
        for code, status in expected.items():
            message = 'A fixed service sentence.'
            service.error = DocumentUploadError(code, message)
            call = _AsgiCall(_build_app(self.h.storage, user=self.alice), document['id'])
            response = await call()
            self.assertEqual(status, response.status_code, code)
            self.assertEqual({'error': message, 'code': code}, response.json(), code)

    async def test_an_unknown_service_code_is_a_fixed_500(self) -> None:
        document = await self._seed_document()
        private = 's3://private/bucket/knowledge/secret.pdf'
        service = _FakeService(error=DocumentUploadError('document.mystery', private))
        self._patch_service(service)
        call = _AsgiCall(_build_app(self.h.storage, user=self.alice), document['id'])
        response = await call()
        self.assertEqual(500, response.status_code)
        self.assertEqual({'error': 'Internal server error', 'code': 'internal_error'}, response.json())
        self.assertNotIn(private, response.text)

    async def test_a_service_cancellation_is_not_swallowed_or_faked(self) -> None:
        document = await self._seed_document()
        service = _FakeService(error=asyncio.CancelledError())
        self._patch_service(service)
        app = _build_app(self.h.storage, user=self.alice)
        with self.assertRaises(asyncio.CancelledError):
            await _AsgiCall(app, document['id'])()
        # Never a fabricated 202, and the gate is released on the way out.
        self.assertEqual(knowledge_api.MAX_ACTIVE_UPLOADS, app.state.knowledge_upload_semaphore._value)


class SlotGateTest(_FileApiTestCase):
    """At most two uploads per app; the third is refused without queueing."""

    async def test_two_held_uploads_make_the_third_429(self) -> None:
        document = await self._seed_document()
        release = asyncio.Event()
        self.addCleanup(release.set)
        service = _FakeService(release=release)
        self._patch_service(service)
        app = _build_app(self.h.storage, user=self.alice)
        first = asyncio.create_task(_AsgiCall(app, document['id'], pieces=[b'a'])())
        second = asyncio.create_task(_AsgiCall(app, document['id'], pieces=[b'b'])())
        await _wait_until(lambda: len(service.started) == 2)

        third = _AsgiCall(app, document['id'], pieces=[b'c'])
        response = await third()
        self.assertEqual(429, response.status_code)
        self.assertEqual('document.upload_busy', response.json()['code'])
        self.assertEqual(0, third.receive_calls)
        self.assertEqual(2, len(service.started))

        release.set()
        self.assertEqual(202, (await first).status_code)
        self.assertEqual(202, (await second).status_code)

    async def test_a_cancelled_request_releases_its_slot(self) -> None:
        document = await self._seed_document()
        release = asyncio.Event()
        self.addCleanup(release.set)
        service = _FakeService(release=release)
        self._patch_service(service)
        app = _build_app(self.h.storage, user=self.alice)
        task = asyncio.create_task(_AsgiCall(app, document['id'], pieces=[b'a'])())
        await _wait_until(service.entered.is_set)
        # One of the two slots is held, so the gate itself is not closed yet.
        self.assertEqual(1, app.state.knowledge_upload_semaphore._value)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(knowledge_api.MAX_ACTIVE_UPLOADS, app.state.knowledge_upload_semaphore._value)

        release.set()
        again = await _AsgiCall(app, document['id'], pieces=[b'b'])()
        self.assertEqual(202, again.status_code)

    async def test_two_apps_do_not_share_the_gate(self) -> None:
        document = await self._seed_document()
        release = asyncio.Event()
        self.addCleanup(release.set)
        holding = _FakeService(release=release)
        passing = _FakeService()
        app_a = _build_app(self.h.storage, user=self.alice)
        app_b = _build_app(self.h.storage, user=self.alice)
        with patch.object(knowledge_api, 'receive_document_file', new=holding):
            first = asyncio.create_task(_AsgiCall(app_a, document['id'], pieces=[b'a'])())
            second = asyncio.create_task(_AsgiCall(app_a, document['id'], pieces=[b'b'])())
            await _wait_until(lambda: len(holding.started) == 2)

            busy = await _AsgiCall(app_a, document['id'], pieces=[b'c'])()
            self.assertEqual(429, busy.status_code)

        # A full gate on A must not close B's own gate.
        with patch.object(knowledge_api, 'receive_document_file', new=passing):
            allowed = await _AsgiCall(app_b, document['id'], pieces=[b'd'])()
        self.assertEqual(202, allowed.status_code)
        self.assertIsNot(app_a.state.knowledge_upload_semaphore, app_b.state.knowledge_upload_semaphore)

        release.set()
        self.assertEqual(202, (await first).status_code)
        self.assertEqual(202, (await second).status_code)


class _FakeStore:
    """Synchronous in-process stand-in for the object-store helpers.

    It hashes and measures the private file for real, exactly like the SDK path
    does, and never touches SQLite: the service hands it work on a worker thread.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
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
        self.caller_is_thread = threading.current_thread() is not threading.main_thread()
        with Path(path).open('rb') as handle:
            payload = handle.read()
        return StoredDocument(
            key=build_document_key(
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                upload_token=upload_token,
                mime_type=mime_type,
            ),
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    def delete_document_object(self, key: str) -> None:
        self.deletes.append(key)


class RealUploadServiceTest(unittest.IsolatedAsyncioTestCase):
    """One PUT through the real service, SQLite double and fake object store."""

    def setUp(self) -> None:
        self.h = _HttpHarness()
        self.alice = User(id=1, username='alice')
        self.store = _FakeStore()
        for name in ('store_document', 'delete_document_object'):
            patched = patch.object(upload_module, name, new=getattr(self.store, name))
            patched.start()
            self.addCleanup(patched.stop)

    async def _seed_document(self) -> dict:
        base = await self.h.repo.create_knowledge_base(1, 'Mine')
        return await self.h.repo.create_document(1, base['id'], filename='a.pdf', mime_type='application/pdf')

    async def test_a_real_upload_queues_the_row_and_hides_the_private_columns(self) -> None:
        document = await self._seed_document()
        call = _AsgiCall(
            _build_app(self.h.storage, user=self.alice),
            document['id'],
            headers={'content-type': 'application/pdf'},
            pieces=[PAYLOAD[:5], PAYLOAD[5:]],
        )
        response = await call()

        self.assertEqual(202, response.status_code)
        public = response.json()['document']
        self.assertEqual(PUBLIC_DOCUMENT_FIELDS, tuple(public))
        self.assertEqual('queued', public['state'])
        self.assertEqual(len(PAYLOAD), public['size_bytes'])
        self.assertEqual(2, call.body_pieces)

        row = self.h.document_row(document['id'])
        self.assertEqual('queued', row['state'])
        self.assertEqual(len(PAYLOAD), row['size_bytes'])
        self.assertEqual(hashlib.sha256(PAYLOAD).hexdigest(), row['sha256'])
        # The store was asked for the row's own canonical type, and the private
        # digest and object key never reach the response.
        self.assertEqual('application/pdf', self.store.calls[0]['mime_type'])
        self.assertEqual(document['id'], self.store.calls[0]['document_id'])
        self.assertNotIn(row['sha256'], response.text)
        self.assertNotIn(row['s3_key'], response.text)
        # The store ran on a worker thread, off the event loop and off SQLite.
        self.assertTrue(self.store.caller_is_thread)

    async def test_the_real_service_counts_bytes_regardless_of_a_small_declared_length(self) -> None:
        document = await self._seed_document()
        call = _AsgiCall(
            _build_app(self.h.storage, user=self.alice),
            document['id'],
            headers={'content-length': '3', 'content-type': 'application/pdf'},
            pieces=[PAYLOAD[:5], PAYLOAD[5:]],
        )
        response = await call()

        self.assertEqual(202, response.status_code)
        row = self.h.document_row(document['id'])
        self.assertEqual(len(PAYLOAD), row['size_bytes'])
        self.assertEqual(hashlib.sha256(PAYLOAD).hexdigest(), row['sha256'])


if __name__ == '__main__':
    unittest.main()
