"""Contract of ``GET /api/document/{id}/content`` and its repository reader.

The route runs over ``httpx.ASGITransport`` against the real
``KnowledgeRepository`` on the in-memory SQLite double, and every document whose
content is read is published through the real upload/import state machine
(``complete_import``), so the paging SQL, the owner filter and the status gate
are actually executed instead of asserted for.

What SQLite cannot cover: it has no row locks and no READ COMMITTED snapshot, so
the paired ``FOR SHARE`` read that ties ``version`` to the chunk slice is only
asserted from the captured statement text and from the plain documents registry
read that must stay unlockable. The PostgreSQL lock behaviour itself is **not
exercised against a real PostgreSQL server**.
"""

from __future__ import annotations

import hashlib
import unittest
from contextlib import AbstractAsyncContextManager
from uuid import uuid4

import httpx
from test_knowledge_api import (
    PUBLIC_DOCUMENT_FIELDS,
    SERVER_OWNED_KEYS,
    SERVER_OWNED_VALUES,
    _client,
    _HttpHarness,
)

from hippo.knowledge.parse import MAX_CHUNK_CHARS, Chunk
from hippo.models import User

#: The public chunk row, restated here so a route echoing ``document_id`` or any
#: other internal column fails this file rather than only review.
PUBLIC_CHUNK_FIELDS = ('page_no', 'ordinal', 'text', 'chunk_type', 'heading_level')


class _ContentHarness(_HttpHarness):
    """The HTTP harness plus the ready-document fixture used by every content test."""

    async def publish(
        self,
        *,
        owner: int = 1,
        base: dict | None = None,
        filename: str = 'a.pdf',
        page_count: int = 1,
        chunks: list[Chunk],
        mime: str = 'application/pdf',
    ) -> dict:
        """Take one document through the real upload/import state machine to ``ready``."""
        if base is None:
            base = await self.repo.create_knowledge_base(owner, 'Papers')
        doc = await self.repo.create_document(owner, base['id'], filename=filename, mime_type=mime)
        token = str(uuid4())
        key = f'knowledge/{base["id"]}/{doc["id"]}/{filename}'
        assert await self.repo.begin_upload(owner, doc['id'], token, s3_key=key)
        digest = hashlib.sha256(doc['id'].encode()).hexdigest()
        assert await self.repo.finish_upload(owner, doc['id'], token, size_bytes=10, sha256=digest) == 'queued'
        assert await self.repo.claim_import(owner, doc['id'], 'worker-1', token) is not None
        assert await self.repo.complete_import(owner, doc['id'], token, page_count=page_count, chunks=chunks)
        return {'base': base, 'doc': doc, 'token': token, 'key': key, 'sha': digest}


class DocumentContentApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.h = _ContentHarness()
        self.alice = User(id=1, username='alice')
        self.bob = User(id=2, username='bob')

    def tearDown(self) -> None:
        self.h.raw.close()

    def acting_as(self, user: User) -> AbstractAsyncContextManager[httpx.AsyncClient]:
        return _client(self.h.storage, user=user)

    async def test_anonymous_is_rejected_by_the_real_dependency(self) -> None:
        async with _client(self.h.storage) as client:
            response = await client.get(f'/api/document/{uuid4()}/content')
        self.assertEqual(401, response.status_code)
        self.assertEqual('auth.session_missing', response.json()['code'])

    async def test_unknown_foreign_malformed_and_deleted_are_404(self) -> None:
        own = await self.h.publish(chunks=[Chunk(1, 0, 'secret derived text')])
        doc_id = own['doc']['id']

        async with self.acting_as(self.bob) as client:
            for bad in (doc_id, 'not-a-uuid', str(uuid4())):
                response = await client.get(f'/api/document/{bad}/content')
                self.assertEqual(404, response.status_code, bad)
                self.assertEqual('document.not_found', response.json()['code'])
                self.assertNotIn('secret derived text', response.text)

        async with self.acting_as(self.alice) as client:
            self.assertEqual(404, (await client.get('/api/document/not-a-uuid/content')).status_code)
            self.assertEqual(404, (await client.get(f'/api/document/{uuid4()}/content')).status_code)
            # The document itself, then its parent base: both tombstones are 404
            # and neither leaks the chunk text that the tombstone dropped.
            parented = await self.h.publish(chunks=[Chunk(1, 0, 'parented text')])
            assert await self.h.repo.delete_document(1, doc_id)
            self.assertEqual(404, (await client.get(f'/api/document/{doc_id}/content')).status_code)
            assert await self.h.repo.delete_knowledge_base(1, parented['base']['id'])
            gone = await client.get(f'/api/document/{parented["doc"]["id"]}/content')
            self.assertEqual(404, gone.status_code)
            self.assertNotIn('parented text', gone.text)

    async def test_non_ready_states_are_409_without_any_content(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Papers')
        for state in ('pending_upload', 'uploading', 'queued', 'processing', 'failed', 'unsupported'):
            doc = await self.h.repo.create_document(1, base['id'], filename=f'{state}.pdf')
            self.h.seed_chunk(doc['id'], 1, 0, 'derived text that must not be returned')
            self.h.raw.execute('UPDATE document SET state = ? WHERE id = ?', (state, doc['id']))
            self.h.raw.commit()
            async with self.acting_as(self.alice) as client:
                response = await client.get(f'/api/document/{doc["id"]}/content')
            self.assertEqual(409, response.status_code, state)
            self.assertEqual('document.not_ready', response.json()['code'])
            self.assertNotIn('derived text', response.text)
            # The 409 is a read gate, never a write: the row is byte-identical.
            self.assertEqual(state, self.h.document_row(doc['id'])['state'])

    async def test_response_uses_the_public_document_and_chunk_whitelist(self) -> None:
        published = await self.h.publish(
            chunks=[
                Chunk(1, 0, 'intro', chunk_type='heading', heading_level=2),
                Chunk(1, 1, 'body'),
            ]
        )
        async with self.acting_as(self.alice) as client:
            response = await client.get(f'/api/document/{published["doc"]["id"]}/content')
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(['document', 'chunks', 'next_offset'], list(body))
        self.assertEqual(PUBLIC_DOCUMENT_FIELDS, tuple(body['document']))
        self.assertEqual(published['doc']['id'], body['document']['id'])
        self.assertEqual(2, body['document']['version'])
        self.assertIsNone(body['next_offset'])
        self.assertEqual(
            [
                {'page_no': 1, 'ordinal': 0, 'text': 'intro', 'chunk_type': 'heading', 'heading_level': 2},
                {'page_no': 1, 'ordinal': 1, 'text': 'body', 'chunk_type': 'paragraph', 'heading_level': None},
            ],
            body['chunks'],
        )
        for chunk in body['chunks']:
            self.assertEqual(PUBLIC_CHUNK_FIELDS, tuple(chunk))
        # The object key, digest, lease pair and owner id are on the internal row
        # the reader returns; none of them may reach the response.
        for secret in (*SERVER_OWNED_KEYS, *SERVER_OWNED_VALUES, published['key'], published['sha']):
            self.assertNotIn(secret, response.text)

    async def test_paging_is_bounded_and_the_next_offset_is_accurate(self) -> None:
        doc = (await self.h.publish(chunks=[Chunk(1, i, f'chunk-{i}') for i in range(3)]))['doc']
        async with self.acting_as(self.alice) as client:
            # A page that is exactly full must not claim a successor: only the
            # ``limit + 1`` probe can tell the two cases apart.
            full = await client.get(f'/api/document/{doc["id"]}/content?limit=3')
            self.assertEqual(3, len(full.json()['chunks']))
            self.assertIsNone(full.json()['next_offset'])

            page = await client.get(f'/api/document/{doc["id"]}/content?limit=2')
            self.assertEqual([0, 1], [chunk['ordinal'] for chunk in page.json()['chunks']])
            self.assertEqual(2, page.json()['next_offset'])

            last = await client.get(f'/api/document/{doc["id"]}/content?limit=2&offset=2')
            self.assertEqual([2], [chunk['ordinal'] for chunk in last.json()['chunks']])
            self.assertIsNone(last.json()['next_offset'])

            default = await client.get(f'/api/document/{doc["id"]}/content')
            self.assertEqual(3, len(default.json()['chunks']))
            self.assertIsNone(default.json()['next_offset'])

            for query in ('limit=0', 'limit=101', 'offset=-1', 'page_no=0', 'page_no=1001', 'page_no=none'):
                self.assertEqual(
                    422, (await client.get(f'/api/document/{doc["id"]}/content?{query}')).status_code, query
                )

        # The same probe at the real maximum: 101 rows with the default page size.
        big = (await self.h.publish(chunks=[Chunk(1, i, f'row-{i}') for i in range(101)]))['doc']
        async with self.acting_as(self.alice) as client:
            capped = await client.get(f'/api/document/{big["id"]}/content')
        self.assertEqual(100, len(capped.json()['chunks']))
        self.assertEqual(100, capped.json()['next_offset'])

    async def test_offset_and_page_filter_with_a_genuinely_empty_page(self) -> None:
        # Page 2 has no chunk at all: a blank PDF page is valid and must answer
        # 200 with an empty array, never a fabricated paragraph.
        doc = (
            await self.h.publish(
                page_count=3,
                chunks=[Chunk(1, 0, 'p1a'), Chunk(1, 1, 'p1b'), Chunk(3, 0, 'p3a')],
            )
        )['doc']
        async with self.acting_as(self.alice) as client:
            across = await client.get(f'/api/document/{doc["id"]}/content?limit=5&offset=1')
            self.assertEqual([(1, 1), (3, 0)], [(c['page_no'], c['ordinal']) for c in across.json()['chunks']])
            self.assertIsNone(across.json()['next_offset'])

            page_two = await client.get(f'/api/document/{doc["id"]}/content?page_no=2')
            self.assertEqual(200, page_two.status_code)
            self.assertEqual([], page_two.json()['chunks'])
            self.assertIsNone(page_two.json()['next_offset'])

            page_three = await client.get(f'/api/document/{doc["id"]}/content?page_no=3&offset=1')
            self.assertEqual([], page_three.json()['chunks'])

            within = await client.get(f'/api/document/{doc["id"]}/content?page_no=1&offset=1&limit=1')
            self.assertEqual([(1, 1)], [(c['page_no'], c['ordinal']) for c in within.json()['chunks']])
            self.assertIsNone(within.json()['next_offset'])

            beyond = await client.get(f'/api/document/{doc["id"]}/content?page_no=4')
            self.assertEqual(404, beyond.status_code)
            self.assertEqual('document.page_not_found', beyond.json()['code'])
            self.assertNotIn('p3a', beyond.text)

    async def test_duplicate_text_keeps_its_page_and_ordinal(self) -> None:
        doc = (
            await self.h.publish(
                page_count=2,
                chunks=[Chunk(1, 0, 'same'), Chunk(1, 1, 'same'), Chunk(2, 0, 'same')],
            )
        )['doc']
        async with self.acting_as(self.alice) as client:
            response = await client.get(f'/api/document/{doc["id"]}/content?limit=10')
        self.assertEqual([(1, 0), (1, 1), (2, 0)], [(c['page_no'], c['ordinal']) for c in response.json()['chunks']])
        self.assertEqual({'same'}, {c['text'] for c in response.json()['chunks']})

    async def test_unicode_and_literal_html_survive_verbatim(self) -> None:
        text = '<script>alert("x")</script> & \u4e2d\u6587\u6d4b\u8bd5 \U0001f600'
        doc = (await self.h.publish(chunks=[Chunk(1, 0, text)]))['doc']
        async with self.acting_as(self.alice) as client:
            response = await client.get(f'/api/document/{doc["id"]}/content')
        self.assertEqual(text, response.json()['chunks'][0]['text'])
        # The text is plain UTF-8, not double-escaped HTML, and is never parsed.
        self.assertIn('<script>', response.text)
        self.assertNotIn('&lt;script&gt;', response.text)
        self.assertNotIn('\\u003cscript', response.text)

    async def test_a_tail_beyond_fifty_thousand_characters_stays_pageable(self) -> None:
        chunks = []
        for ordinal in range(13):
            prefix = f'block-{ordinal:02d}-'
            chunks.append(Chunk(1, ordinal, prefix + 'x' * (MAX_CHUNK_CHARS - len(prefix))))
        self.assertGreater(sum(len(chunk.text) for chunk in chunks), 50_000)
        doc = (await self.h.publish(chunks=chunks))['doc']
        async with self.acting_as(self.alice) as client:
            tail = await client.get(f'/api/document/{doc["id"]}/content?offset=12&limit=5')
        self.assertEqual([12], [chunk['ordinal'] for chunk in tail.json()['chunks']])
        self.assertTrue(tail.json()['chunks'][0]['text'].startswith('block-12-'))
        self.assertEqual(MAX_CHUNK_CHARS, len(tail.json()['chunks'][0]['text']))
        self.assertIsNone(tail.json()['next_offset'])

    async def test_reading_content_changes_no_counters_or_source(self) -> None:
        published = await self.h.publish(chunks=[Chunk(1, 0, 'text')])
        doc_id = published['doc']['id']
        before = dict(self.h.document_row(doc_id))
        self.assertEqual(1, self.h.count('SELECT COUNT(*) FROM document_chunk WHERE document_id = ?', (doc_id,)))

        async with self.acting_as(self.alice) as client:
            self.assertEqual(200, (await client.get(f'/api/document/{doc_id}/content')).status_code)

        after = dict(self.h.document_row(doc_id))
        self.assertEqual(before, after)
        self.assertEqual(1, self.h.count('SELECT COUNT(*) FROM document_chunk WHERE document_id = ?', (doc_id,)))

    async def test_reader_sql_locks_only_the_document_and_reuses_owner_exists(self) -> None:
        published = await self.h.publish(chunks=[Chunk(1, 0, 'one'), Chunk(1, 1, 'two')])
        doc_id = published['doc']['id']

        start = len(self.h.executed)
        result = await self.h.repo.read_document_content(1, doc_id, limit=1)
        # The reader returns the ``limit + 1`` probe row too; trimming to the page
        # is the caller's job, which is what keeps a full last page honest.
        self.assertEqual(['one', 'two'], [row['text'] for row in result['chunks']])
        # The internal row is what the reader hands back; the projection is the API's job.
        self.assertEqual(published['key'], result['document']['s3_key'])
        document_sql = self.h.executed[start]
        chunk_sql = self.h.executed[start + 1]
        # The lock is taken on the document alone: ownership is an EXISTS on the
        # base, so the FROM list never joins the parent and cannot inherit the
        # delete's base-then-document lock order.
        self.assertIn('FOR SHARE', document_sql)
        self.assertIn('EXISTS', document_sql)
        self.assertIn('kb.user_id = %s', document_sql)
        self.assertIn('kb.deleted_at IS NULL', document_sql)
        self.assertIn('d.deleted_at IS NULL', document_sql)
        self.assertNotIn('JOIN', document_sql)
        self.assertNotIn('JOIN', chunk_sql)
        self.assertIn('ORDER BY c.page_no ASC, c.ordinal ASC', chunk_sql)
        self.assertIn('LIMIT %s OFFSET %s', chunk_sql)
        self.assertNotIn('FOR SHARE', chunk_sql)

        # A page filter is a bound parameter on the same statement.
        start = len(self.h.executed)
        await self.h.repo.read_document_content(1, doc_id, page_no=1)
        self.assertIn('c.page_no = %s', self.h.executed[start + 1])

        # The plain metadata read must stay unlockable: a lock there would hold a
        # share on the row for the whole request.
        start = len(self.h.executed)
        await self.h.repo.get_document(1, doc_id)
        plain = self.h.executed[start]
        self.assertIn('JOIN knowledge_base', plain)
        self.assertNotIn('FOR SHARE', plain)
        self.assertNotIn('FOR UPDATE', plain)

    async def test_reader_returns_the_internal_doc_and_no_chunks_when_not_ready(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Papers')
        pending = await self.h.repo.create_document(1, base['id'], filename='p.pdf')
        self.h.seed_chunk(pending['id'], 1, 0, 'unpublished')

        result = await self.h.repo.read_document_content(1, pending['id'])
        self.assertEqual({'document', 'chunks'}, set(result))
        self.assertEqual('pending_upload', result['document']['state'])
        self.assertEqual([], result['chunks'])
        # The API decides the 409, not the reader.

        published = await self.h.publish(base=base, chunks=[Chunk(1, 0, 'live')])
        ready = await self.h.repo.read_document_content(1, published['doc']['id'])
        self.assertEqual('ready', ready['document']['state'])
        self.assertEqual(['live'], [row['text'] for row in ready['chunks']])
        self.assertEqual(['page_no', 'ordinal', 'text', 'chunk_type', 'heading_level'], list(ready['chunks'][0]))

        # A foreign owner and a tombstone are misses; a malformed id never reaches SQL.
        self.assertIsNone(await self.h.repo.read_document_content(2, published['doc']['id']))
        assert await self.h.repo.delete_document(1, published['doc']['id'])
        self.assertIsNone(await self.h.repo.read_document_content(1, published['doc']['id']))
        start = len(self.h.executed)
        self.assertIsNone(await self.h.repo.read_document_content(1, 'not-a-uuid'))
        self.assertIsNone(await self.h.repo.read_document_content(1, None))
        self.assertEqual(start, len(self.h.executed))

    async def test_reader_rejects_a_bad_page_no_before_sql(self) -> None:
        published = await self.h.publish(chunks=[Chunk(1, 0, 'text')])
        start = len(self.h.executed)
        for bad in (0, -1, True, False, '2', 1.0):
            with self.assertRaises(ValueError, msg=f'accepted page_no={bad!r}'):
                await self.h.repo.read_document_content(1, published['doc']['id'], page_no=bad)  # type: ignore[arg-type]
        self.assertEqual(start, len(self.h.executed))


if __name__ == '__main__':
    unittest.main()
