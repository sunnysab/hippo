"""HTTP contract of the knowledge-base metadata routes.

The routes run over ``httpx.ASGITransport`` against the real
``KnowledgeRepository`` on the in-memory SQLite double borrowed from
``test_knowledge_repository``. Only the storage handle is faked, and only for the
two things these routes need: ``knowledge`` and an explicit ``transaction()``
that really commits or rolls back, so a route that forgets the commit is caught.

``current_user`` is deliberately *not* overridden in the anonymous case: that 401
comes from the real dependency chain, so the suite never bypasses authentication
and then claims it is covered.

Not covered here: PostgreSQL row locks, the ``jiebaqry`` full-text pieces and
anything touching S3 or the worker - none of which these routes reach.
"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI
from test_knowledge_repository import _AsyncConnection, _Harness

from hippo.api.deps import current_user, get_storage
from hippo.api.errors import VALIDATION_FAILED, install_exception_handlers
from hippo.api.routers import knowledge as knowledge_api
from hippo.models import User
from hippo.repositories.knowledge import KnowledgeRepository

#: The public contract of each response, restated here on purpose: a route that
#: starts echoing ``s3_key`` or ``user_id`` must fail this file, not just review.
PUBLIC_KNOWLEDGE_BASE_FIELDS = ('id', 'name', 'created_at', 'updated_at')
PUBLIC_DOCUMENT_FIELDS = (
    'id',
    'knowledge_base_id',
    'filename',
    'mime_type',
    'size_bytes',
    'state',
    'attempts',
    'error_code',
    'error_message',
    'page_count',
    'version',
    'created_at',
    'updated_at',
)
#: Row columns that must never appear as a response key.
SERVER_OWNED_KEYS = ('s3_key', 'sha256', 'deleted_at', 'locked_by', 'lease_token', 'locked_at', 'user_id')
#: Values written into those columns; none may be echoed either, since a leaked
#: object key or digest in any field would still be a leak.
SERVER_OWNED_VALUES = ('knowledge/secret-object.pdf', 'a' * 64, 'worker-1')


class _TxConnection(_AsyncConnection):
    """The SQLite double plus the async commit/rollback ``StorageTransaction`` calls."""

    async def commit(self) -> None:
        self._conn.commit()

    async def rollback(self) -> None:
        self._conn.rollback()


class _FakeStorage:
    """The slice of ``PostgresStorage`` these routes use."""

    def __init__(self, conn: _TxConnection) -> None:
        self._conn = conn
        self.knowledge = KnowledgeRepository(conn)  # type: ignore[arg-type]
        # Only reached by the real ``current_user_optional`` on the anonymous path.
        self.sessions = SimpleNamespace(resolve=AsyncMock(return_value=None))
        self.commits = 0
        self.rollbacks = 0

    @asynccontextmanager
    async def transaction(self):
        try:
            yield self
        except BaseException:
            self.rollbacks += 1
            await self._conn.rollback()
            raise
        self.commits += 1
        await self._conn.commit()


class _HttpHarness(_Harness):
    """Repository harness whose connection the storage handle can also commit through."""

    def __init__(self) -> None:
        super().__init__()
        self.connection = _TxConnection(self.raw)
        self.repo = KnowledgeRepository(self.connection)  # type: ignore[arg-type]
        self.storage = _FakeStorage(self.connection)


@asynccontextmanager
async def _client(
    storage: _FakeStorage,
    *,
    user: User | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """An ASGI client over the router, with storage - and optionally the user - stubbed."""
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(knowledge_api.router, prefix='/api')

    async def _fake_storage() -> _FakeStorage:
        return storage

    app.dependency_overrides[get_storage] = _fake_storage
    if user is not None:
        app.dependency_overrides[current_user] = lambda: user
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url='http://hippo.test',
    ) as client:
        yield client


class KnowledgeBaseApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.h = _HttpHarness()
        self.alice = User(id=1, username='alice')
        self.bob = User(id=2, username='bob')

    def acting_as(self, user: User) -> AbstractAsyncContextManager[httpx.AsyncClient]:
        return _client(self.h.storage, user=user)

    async def _create_base(self, client, name: str = 'Mine') -> str:
        response = await client.post('/api/knowledge-base', json={'name': name})
        self.assertEqual(201, response.status_code)
        return response.json()['knowledge_base']['id']

    async def _create_document(
        self,
        client,
        knowledge_base_id: str,
        filename: str = 'a.pdf',
        mime_type: str = 'application/pdf',
    ) -> dict:
        response = await client.post(
            f'/api/knowledge-base/{knowledge_base_id}/document',
            json={'filename': filename, 'mime_type': mime_type},
        )
        self.assertEqual(201, response.status_code)
        return response.json()['document']

    async def test_anonymous_is_rejected_by_the_real_dependency(self) -> None:
        async with _client(self.h.storage) as client:
            listed = await client.get('/api/knowledge-base')
            written = await client.post('/api/knowledge-base', json={'name': 'Papers'})
        for response in (listed, written):
            self.assertEqual(401, response.status_code)
            self.assertEqual('auth.session_missing', response.json()['code'])
        self.assertEqual(0, self.h.storage.commits)
        self.assertEqual(0, self.h.count('SELECT COUNT(*) FROM knowledge_base'))

    async def test_owner_crud_lifecycle(self) -> None:
        async with self.acting_as(self.alice) as client:
            created = await client.post('/api/knowledge-base', json={'name': 'Papers'})
            self.assertEqual(201, created.status_code)
            knowledge_base = created.json()['knowledge_base']
            self.assertEqual(PUBLIC_KNOWLEDGE_BASE_FIELDS, tuple(knowledge_base))
            self.assertEqual('Papers', knowledge_base['name'])
            for key in (*SERVER_OWNED_KEYS, *SERVER_OWNED_VALUES):
                self.assertNotIn(key, created.text)
            kb_id = knowledge_base['id']
            listed = await client.get('/api/knowledge-base')
            self.assertEqual(200, listed.status_code)
            self.assertEqual(['knowledge_bases', 'limit', 'offset'], list(listed.json()))
            self.assertEqual([kb_id], [row['id'] for row in listed.json()['knowledge_bases']])
            self.assertEqual(PUBLIC_KNOWLEDGE_BASE_FIELDS, tuple(listed.json()['knowledge_bases'][0]))
            self.assertEqual({'limit': 50, 'offset': 0}, {k: listed.json()[k] for k in ('limit', 'offset')})

            fetched = await client.get(f'/api/knowledge-base/{kb_id}')
            self.assertEqual(200, fetched.status_code)
            self.assertEqual(knowledge_base, fetched.json()['knowledge_base'])

            renamed = await client.patch(f'/api/knowledge-base/{kb_id}', json={'name': 'Reading'})
            self.assertEqual(200, renamed.status_code)
            self.assertEqual('Reading', renamed.json()['knowledge_base']['name'])

            deleted = await client.delete(f'/api/knowledge-base/{kb_id}')
            self.assertEqual(204, deleted.status_code)
            self.assertEqual(b'', deleted.content)

            # A tombstone, not a physical delete: the row is still there.
            row = self.h.raw.execute('SELECT name, deleted_at FROM knowledge_base WHERE id = ?', (kb_id,)).fetchone()
            self.assertEqual('Reading', row[0])
            self.assertIsNotNone(row[1])
            self.assertEqual(404, (await client.get(f'/api/knowledge-base/{kb_id}')).status_code)
            no_second_rename = await client.patch(f'/api/knowledge-base/{kb_id}', json={'name': 'X'})
            self.assertEqual(404, no_second_rename.status_code)
            self.assertEqual(404, (await client.delete(f'/api/knowledge-base/{kb_id}')).status_code)
            self.assertEqual([], (await client.get('/api/knowledge-base')).json()['knowledge_bases'])
        # create, rename and delete committed; the two 404 writes rolled back.
        self.assertEqual(3, self.h.storage.commits)
        self.assertEqual(2, self.h.storage.rollbacks)

    async def test_foreign_bad_and_deleted_ids_are_404(self) -> None:
        async with self.acting_as(self.alice) as client:
            kb_id = await self._create_base(client, 'Mine')
            doc_id = (await self._create_document(client, kb_id))['id']

        async with self.acting_as(self.bob) as client:
            for path in (f'/api/knowledge-base/{kb_id}', f'/api/knowledge-base/{kb_id}/document'):
                self.assertEqual(404, (await client.get(path)).status_code)
            self.assertEqual(404, (await client.get(f'/api/document/{doc_id}')).status_code)
            self.assertEqual(
                404, (await client.patch(f'/api/knowledge-base/{kb_id}', json={'name': 'Steal'})).status_code
            )
            self.assertEqual(404, (await client.delete(f'/api/knowledge-base/{kb_id}')).status_code)
            self.assertEqual(404, (await client.delete(f'/api/document/{doc_id}')).status_code)
            self.assertEqual(
                404,
                (
                    await client.post(
                        f'/api/knowledge-base/{kb_id}/document',
                        json={'filename': 'x.pdf', 'mime_type': 'text/plain'},
                    )
                ).status_code,
            )

        # Nothing the other user attempted changed the owner's data.
        self.assertEqual(
            'Mine',
            self.h.raw.execute('SELECT name FROM knowledge_base WHERE id = ?', (kb_id,)).fetchone()[0],
        )
        self.assertEqual(1, self.h.count('SELECT COUNT(*) FROM document WHERE deleted_at IS NULL'))

        async with self.acting_as(self.alice) as client:
            for bad in ('not-a-uuid', str(uuid4()), '0'):
                self.assertEqual(404, (await client.get(f'/api/knowledge-base/{bad}')).status_code)
                self.assertEqual(404, (await client.get(f'/api/knowledge-base/{bad}/document')).status_code)
                self.assertEqual(404, (await client.delete(f'/api/knowledge-base/{bad}')).status_code)
            self.assertEqual(404, (await client.get(f'/api/document/{uuid4()}')).status_code)
            self.assertIsNone(self.h.raw.execute('SELECT 1 FROM knowledge_base WHERE id = ?', ('0',)).fetchone())

    async def test_document_metadata_lifecycle_and_field_whitelist(self) -> None:
        async with self.acting_as(self.alice) as client:
            kb_id = await self._create_base(client, 'Mine')
            document = await self._create_document(client, kb_id)
            self.assertEqual(PUBLIC_DOCUMENT_FIELDS, tuple(document))
            self.assertEqual('pending_upload', document['state'])
            self.assertEqual('application/pdf', document['mime_type'])
            self.assertIsNone(document['size_bytes'])
            self.assertEqual(1, document['version'])
            doc_id = document['id']

            # Later packages fill the internal columns; none of them may leak.
            self.h.raw.execute(
                'UPDATE document SET s3_key = ?, sha256 = ?, locked_by = ? WHERE id = ?',
                (SERVER_OWNED_VALUES[0], SERVER_OWNED_VALUES[1], SERVER_OWNED_VALUES[2], doc_id),
            )
            self.h.raw.commit()
            fetched = await client.get(f'/api/document/{doc_id}')
            self.assertEqual(200, fetched.status_code)
            self.assertEqual(document, fetched.json()['document'])

            listed = await client.get(f'/api/knowledge-base/{kb_id}/document')
            self.assertEqual(200, listed.status_code)
            self.assertEqual(['documents', 'limit', 'offset'], list(listed.json()))
            self.assertEqual([doc_id], [row['id'] for row in listed.json()['documents']])
            for body in (fetched.text, listed.text):
                for key in (*SERVER_OWNED_KEYS, *SERVER_OWNED_VALUES):
                    self.assertNotIn(key, body)

            deleted = await client.delete(f'/api/document/{doc_id}')
            self.assertEqual(204, deleted.status_code)
            self.assertEqual(404, (await client.get(f'/api/document/{doc_id}')).status_code)
            self.assertEqual(404, (await client.delete(f'/api/document/{doc_id}')).status_code)
            self.assertEqual([], (await client.get(f'/api/knowledge-base/{kb_id}/document')).json()['documents'])

        # Tombstone only: the row and its object key stay for the cleanup job.
        row = self.h.document_row(doc_id)
        self.assertEqual('deleted', row['state'])
        self.assertIsNotNone(row['deleted_at'])
        self.assertEqual(SERVER_OWNED_VALUES[0], row['s3_key'])

    async def test_soft_deleting_a_base_hides_its_documents(self) -> None:
        async with self.acting_as(self.alice) as client:
            kb_id = await self._create_base(client, 'Mine')
            document = await self._create_document(client, kb_id)
            doc_id = document['id']
            # A later upload package would have journalled an object key; the base
            # delete must keep it so a cleanup job can still find the object.
            self.h.raw.execute('UPDATE document SET s3_key = ? WHERE id = ?', (SERVER_OWNED_VALUES[0], doc_id))
            self.h.raw.commit()

            start = len(self.h.executed)
            self.assertEqual(204, (await client.delete(f'/api/knowledge-base/{kb_id}')).status_code)

            # A tombstoned parent answers 404 on read *and* write, never an empty page.
            self.assertEqual(404, (await client.get(f'/api/knowledge-base/{kb_id}/document')).status_code)
            created = await client.post(
                f'/api/knowledge-base/{kb_id}/document',
                json={'filename': 'after.pdf', 'mime_type': 'application/pdf'},
            )
            self.assertEqual(404, created.status_code)
            self.assertEqual(404, (await client.get(f'/api/document/{doc_id}')).status_code)

        # The sweep is the repository's three statements, in order, in one transaction:
        # parent tombstone first (it is the row lock), then the document sweep, then chunks.
        sweep = self.h.executed[start : start + 3]
        self.assertIn('UPDATE knowledge_base SET deleted_at', sweep[0])
        self.assertIn('UPDATE document SET deleted_at', sweep[1])
        self.assertIn('DELETE FROM document_chunk', sweep[2])
        self.assertEqual(1, self.h.count('SELECT COUNT(*) FROM document'))
        row = self.h.document_row(doc_id)
        self.assertEqual('deleted', row['state'])
        self.assertIsNotNone(row['deleted_at'])
        # Tombstone only: the row keeps its object key so cleanup can still find it.
        self.assertEqual(SERVER_OWNED_VALUES[0], row['s3_key'])

    async def test_server_owned_fields_cannot_be_injected(self) -> None:
        # The request models are closed, so an undeclared field - and with it any
        # server-owned column - is refused as a validation failure before the
        # route (and the repository) ever sees it.
        async with self.acting_as(self.alice) as client:
            kb_id = await self._create_base(client, 'Mine')
            for field, value in (
                ('id', str(uuid4())),
                ('user_id', 2),
                ('deleted_at', '2020-01-01T00:00:00+00:00'),
                ('created_at', '2020-01-01T00:00:00+00:00'),
                ('unknown', 1),
            ):
                response = await client.post('/api/knowledge-base', json={'name': 'Papers', field: value})
                self.assertEqual(422, response.status_code, field)
                self.assertEqual(VALIDATION_FAILED, response.json()['code'])
            patched = await client.patch(f'/api/knowledge-base/{kb_id}', json={'name': 'X', 'user_id': 2})
            self.assertEqual(422, patched.status_code)
            self.assertEqual(VALIDATION_FAILED, patched.json()['code'])
            self.assertEqual(
                'Mine', (await client.get(f'/api/knowledge-base/{kb_id}')).json()['knowledge_base']['name']
            )

            for field, value in (
                ('id', str(uuid4())),
                ('knowledge_base_id', str(uuid4())),
                ('user_id', 2),
                ('state', 'ready'),
                ('sha256', SERVER_OWNED_VALUES[1]),
                ('size_bytes', 12),
                ('s3_key', SERVER_OWNED_VALUES[0]),
                ('lease_token', str(uuid4())),
                ('locked_by', 'upload'),
                ('version', 9),
                ('deleted_at', '2020-01-01T00:00:00+00:00'),
                ('unknown', 1),
            ):
                response = await client.post(
                    f'/api/knowledge-base/{kb_id}/document',
                    json={'filename': 'a.pdf', 'mime_type': 'application/pdf', field: value},
                )
                self.assertEqual(422, response.status_code, field)
                self.assertEqual(VALIDATION_FAILED, response.json()['code'])

        self.assertEqual(1, self.h.count('SELECT COUNT(*) FROM knowledge_base'))
        self.assertEqual(0, self.h.count('SELECT COUNT(*) FROM document'))

    async def test_mime_type_must_be_canonical(self) -> None:
        async with self.acting_as(self.alice) as client:
            kb_id = await self._create_base(client, 'Mine')
            for mime_type in (
                '',
                'image/png',
                'application/pdf; charset=utf-8',
                'APPLICATION/PDF',
                ' application/pdf',
            ):
                response = await client.post(
                    f'/api/knowledge-base/{kb_id}/document',
                    json={'filename': 'a.pdf', 'mime_type': mime_type},
                )
                self.assertEqual(415, response.status_code, mime_type)
                self.assertEqual('document.unsupported_mime', response.json()['code'])
            for body in (
                {'filename': 'a.pdf'},
                {'filename': 'a.pdf', 'mime_type': None},
                {'filename': 'a.pdf', 'mime_type': 7},
            ):
                response = await client.post(f'/api/knowledge-base/{kb_id}/document', json=body)
                self.assertEqual(422, response.status_code, body)
                self.assertEqual(VALIDATION_FAILED, response.json()['code'])
            # Both accepted types are stored in their canonical spelling, so a
            # valid TXT upload must send text/plain explicitly rather than rely on
            # the filename.
            accepted = await self._create_document(client, kb_id, 'b.txt', mime_type='text/plain')
            self.assertEqual('text/plain', accepted['mime_type'])
            self.assertEqual(1, self.h.count('SELECT COUNT(*) FROM document'))

    async def test_names_and_filenames_are_validated_by_the_repository(self) -> None:
        async with self.acting_as(self.alice) as client:
            # A missing or non-string field is a request-shape failure (422); a
            # blank or over-long one is the repository's own 400.
            for payload in ({}, {'name': 7}):
                response = await client.post('/api/knowledge-base', json=payload)
                self.assertEqual(422, response.status_code, payload)
                self.assertEqual(VALIDATION_FAILED, response.json()['code'])
            for payload in ({'name': ''}, {'name': '   '}, {'name': 'x' * 201}):
                response = await client.post('/api/knowledge-base', json=payload)
                self.assertEqual(400, response.status_code, payload)
                self.assertEqual('knowledge.invalid_input', response.json()['code'])
            kb_id = await self._create_base(client, 'Mine')
            for payload in ({'mime_type': 'application/pdf'}, {'filename': 7, 'mime_type': 'application/pdf'}):
                response = await client.post(f'/api/knowledge-base/{kb_id}/document', json=payload)
                self.assertEqual(422, response.status_code, payload)
                self.assertEqual(VALIDATION_FAILED, response.json()['code'])
            for payload in (
                {'filename': '', 'mime_type': 'application/pdf'},
                {'filename': '   ', 'mime_type': 'application/pdf'},
                {'filename': 'x' * 256, 'mime_type': 'application/pdf'},
            ):
                response = await client.post(f'/api/knowledge-base/{kb_id}/document', json=payload)
                self.assertEqual(400, response.status_code, payload)
                self.assertEqual('knowledge.invalid_input', response.json()['code'])

            renamed = await client.patch(f'/api/knowledge-base/{kb_id}', json={'name': 'x' * 201})
            self.assertEqual(400, renamed.status_code)
            self.assertEqual(
                'Mine', (await client.get(f'/api/knowledge-base/{kb_id}')).json()['knowledge_base']['name']
            )
        # Every rejected write rolled back; the repository error never reached SQL.
        self.assertEqual(0, self.h.count('SELECT COUNT(*) FROM document'))

    async def test_a_patch_must_name_the_field_it_changes(self) -> None:
        async with self.acting_as(self.alice) as client:
            kb_id = await self._create_base(client, 'Mine')
            # ``name`` is optional in the model so an omitted field and an explicit
            # null stay different: both are refused, and neither renames the base.
            for payload in ({}, {'name': None}):
                response = await client.patch(f'/api/knowledge-base/{kb_id}', json=payload)
                self.assertEqual(400, response.status_code, payload)
                self.assertEqual('knowledge.invalid_input', response.json()['code'])
            extra = await client.patch(f'/api/knowledge-base/{kb_id}', json={'name': 'X', 'user_id': 2})
            self.assertEqual(422, extra.status_code)
            self.assertEqual(VALIDATION_FAILED, extra.json()['code'])
            self.assertEqual(
                'Mine', (await client.get(f'/api/knowledge-base/{kb_id}')).json()['knowledge_base']['name']
            )

    async def test_pagination_is_bounded_and_offset_safe(self) -> None:
        async with self.acting_as(self.alice) as client:
            kb_id = await self._create_base(client, 'Mine')
            for index in range(3):
                await self._create_document(client, kb_id, f'{index}.pdf')

            for query in ('limit=0', 'limit=201', 'limit=-1', 'offset=-1', 'limit=none'):
                for path in ('/api/knowledge-base', f'/api/knowledge-base/{kb_id}/document'):
                    response = await client.get(f'{path}?{query}')
                    self.assertEqual(422, response.status_code, f'{path}?{query}')

            bounded = await client.get(f'/api/knowledge-base/{kb_id}/document?limit=200&offset=0')
            self.assertEqual({'limit': 200, 'offset': 0}, {k: bounded.json()[k] for k in ('limit', 'offset')})
            self.assertEqual(3, len(bounded.json()['documents']))

            page = await client.get(f'/api/knowledge-base/{kb_id}/document?limit=1&offset=2')
            self.assertEqual(1, len(page.json()['documents']))
            beyond = await client.get(f'/api/knowledge-base/{kb_id}/document?limit=1&offset=3')
            self.assertEqual([], beyond.json()['documents'])
            default_page = await client.get(f'/api/knowledge-base/{kb_id}/document')
            self.assertEqual({'limit': 50, 'offset': 0}, {k: default_page.json()[k] for k in ('limit', 'offset')})

    async def test_writes_commit_and_failed_writes_roll_back(self) -> None:
        async with self.acting_as(self.alice) as client:
            kb_id = await self._create_base(client, 'Papers')
            self.assertEqual(1, self.h.storage.commits)
            self.assertEqual(0, self.h.storage.rollbacks)

            # A repository rejection inside the block rolls back instead of committing.
            rejected = await client.patch(f'/api/knowledge-base/{kb_id}', json={'name': '   '})
            self.assertEqual(400, rejected.status_code)
            self.assertEqual(1, self.h.storage.commits)
            self.assertEqual(1, self.h.storage.rollbacks)

            # A 404 on a write rolls back too, so nothing half-applied is persisted.
            missing = await client.delete(f'/api/knowledge-base/{uuid4()}')
            self.assertEqual(404, missing.status_code)
            self.assertEqual((1, 2), (self.h.storage.commits, self.h.storage.rollbacks))

            # A two-statement create (INSERT then the read-back) is still one commit,
            # and the multi-statement sweep of a base delete is another.
            before = self.h.storage.commits
            await self._create_document(client, kb_id)
            self.assertEqual(204, (await client.delete(f'/api/knowledge-base/{kb_id}')).status_code)
            self.assertEqual(before + 2, self.h.storage.commits)
            self.assertEqual(2, self.h.storage.rollbacks)

    async def test_a_body_that_is_not_an_object_is_rejected(self) -> None:
        async with self.acting_as(self.alice) as client:
            for payload in ([], 'name', 7):
                response = await client.post('/api/knowledge-base', json=payload)
                self.assertEqual(422, response.status_code, payload)
        self.assertEqual(0, self.h.count('SELECT COUNT(*) FROM knowledge_base'))


class KnowledgeRouterShapeTest(unittest.TestCase):
    """The router exposes exactly the nine metadata routes, the raw upload, the retry,
    the parsed-content read and the knowledge search: thirteen path/method
    combinations, each registered once."""

    #: The metadata contract, as declared by the router module itself.
    DECLARED: ClassVar[frozenset[tuple[str, str]]] = frozenset(
        {
            ('/knowledge-base', 'GET'),
            ('/knowledge-base', 'POST'),
            ('/knowledge-base/{knowledge_base_id}', 'GET'),
            ('/knowledge-base/{knowledge_base_id}', 'PATCH'),
            ('/knowledge-base/{knowledge_base_id}', 'DELETE'),
            ('/knowledge-base/{knowledge_base_id}/document', 'GET'),
            ('/knowledge-base/{knowledge_base_id}/document', 'POST'),
            ('/document/{document_id}', 'GET'),
            ('/document/{document_id}', 'DELETE'),
        }
    )

    #: The raw-upload route, the failed-document retry, the parsed-content read
    #: and the knowledge search; separate from the metadata set.
    UPLOAD: ClassVar[tuple[str, str]] = ('/document/{document_id}/file', 'PUT')
    RETRY: ClassVar[tuple[str, str]] = ('/document/{document_id}/retry', 'POST')
    CONTENT: ClassVar[tuple[str, str]] = ('/document/{document_id}/content', 'GET')
    SEARCH: ClassVar[tuple[str, str]] = ('/knowledge-base/{knowledge_base_id}/search', 'GET')

    #: The four non-metadata routes this package adds.
    EXTRAS: ClassVar[frozenset[tuple[str, str]]] = frozenset({UPLOAD, RETRY, CONTENT, SEARCH})

    @staticmethod
    def _methods(route) -> set[str]:
        # Starlette may add HEAD (and CORS adds OPTIONS in create_app); those are
        # transport concerns, not routes this package declares.
        return {method for method in getattr(route, 'methods', ()) or () if method not in {'HEAD', 'OPTIONS'}}

    def test_router_paths_and_methods(self) -> None:
        routes = [(route.path, method) for route in knowledge_api.router.routes for method in self._methods(route)]
        # Each declared pair appears exactly once: a duplicated registration is a
        # defect even when the path/method set still matches.
        for pair in self.DECLARED | self.EXTRAS:
            self.assertEqual(1, routes.count(pair), pair)
        self.assertEqual(self.DECLARED | self.EXTRAS, set(routes))

    def test_create_app_registers_the_metadata_router(self) -> None:
        from hippo import server

        with (
            tempfile.TemporaryDirectory() as static_dir,
            # No lifespan and no logging/telemetry setup: this reads the app's
            # routing table and nothing else.
            patch('hippo.server.configure_logging'),
            patch('hippo.server.init_telemetry', return_value=None),
            patch('hippo.server.instrument_app'),
        ):
            app = server.create_app(static_dir=static_dir, enable_inprocess_sync=False)
        registered = {(route.path, method) for route in app.routes for method in self._methods(route)}
        expected = {(f'/api{path}', method) for path, method in self.DECLARED | self.EXTRAS}
        missing = expected - registered
        self.assertEqual(set(), missing, f'not registered under /api: {sorted(missing)}')


if __name__ == '__main__':
    unittest.main()
