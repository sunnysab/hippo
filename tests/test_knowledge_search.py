"""Contract of ``GET /api/knowledge-base/{id}/search``.

Authentication, the base/document ownership and status gates run against the
real repository on the in-memory SQLite double, so a forged scope really is
rejected by the SQL. The full-text statement itself needs PostgreSQL's
``jiebaqry`` dictionary, so only ``search_chunks`` is replaced by an explicit
``AsyncMock``: the awaited call proves what the route asks the repository for
(owner, canonical filters, ``limit + 1`` probe, offset and the trimmed query) and
the response proves the public projection. No SQL is rewritten to pretend SQLite
is a Chinese full-text engine, and the real PostgreSQL ranking/ordering is **not
exercised here**.
"""

from __future__ import annotations

import unittest
from contextlib import AbstractAsyncContextManager
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from test_knowledge_api import (
    SERVER_OWNED_KEYS,
    SERVER_OWNED_VALUES,
    _client,
)
from test_knowledge_content import _ContentHarness

from hippo.knowledge.parse import Chunk
from hippo.models import User
from hippo.repositories.knowledge import MAX_OFFSET

#: The public search hit, restated here so a route echoing ``user_id``,
#: ``s3_key`` or any other storage column fails this file rather than only review.
PUBLIC_SEARCH_HIT_FIELDS = (
    'document_id',
    'knowledge_base_id',
    'filename',
    'version',
    'page_no',
    'ordinal',
    'text',
    'chunk_type',
    'heading_level',
)


class KnowledgeSearchApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.h = _ContentHarness()
        # Only this one repository call is mocked: everything the route does
        # before it (base lookup, document ownership, readiness) is real SQL.
        self.search = AsyncMock(return_value=[])
        self._patcher = patch.object(self.h.storage.knowledge, 'search_chunks', new=self.search)
        self._patcher.start()
        self.alice = User(id=1, username='alice')
        self.bob = User(id=2, username='bob')
        self.admin = User(id=3, username='root', role='admin')

    def tearDown(self) -> None:
        self._patcher.stop()
        self.h.raw.close()

    def acting_as(self, user: User) -> AbstractAsyncContextManager[httpx.AsyncClient]:
        return _client(self.h.storage, user=user)

    @staticmethod
    def _row(base_id: str, doc_id: str, **overrides: object) -> dict:
        """One repository row shape, including the internal columns a leak would show."""
        row: dict[str, object] = {
            'document_id': doc_id,
            'knowledge_base_id': base_id,
            'filename': 'a.pdf',
            'version': 2,
            'page_no': 1,
            'ordinal': 0,
            'text': 'alpha',
            'chunk_type': 'paragraph',
            'heading_level': None,
            # Never part of the response: the object key, digest, lease and owner.
            's3_key': SERVER_OWNED_VALUES[0],
            'sha256': SERVER_OWNED_VALUES[1],
            'locked_by': SERVER_OWNED_VALUES[2],
            'lease_token': str(uuid4()),
            'user_id': 1,
            'deleted_at': None,
            'attempts': 3,
        }
        row.update(overrides)
        return row

    async def test_anonymous_is_rejected_by_the_real_dependency(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Mine')
        self.h.raw.commit()
        async with _client(self.h.storage) as client:
            response = await client.get(f'/api/knowledge-base/{base["id"]}/search?q=term')
        self.assertEqual(401, response.status_code)
        self.assertEqual('auth.session_missing', response.json()['code'])
        self.search.assert_not_awaited()

    async def test_query_is_required_bounded_and_not_blank(self) -> None:
        async with self.acting_as(self.alice) as client:
            base = await self.h.repo.create_knowledge_base(1, 'Mine')
            self.h.raw.commit()
            path = f'/api/knowledge-base/{base["id"]}/search'
            # Missing and empty are FastAPI 422s; blank-but-present is our own 400.
            self.assertEqual(422, (await client.get(path)).status_code)
            self.assertEqual(422, (await client.get(f'{path}?q=')).status_code)
            self.assertEqual(422, (await client.get(f'{path}?q=' + 'x' * 501)).status_code)
            for blank in ('%20%20%20', '%09%0A'):
                response = await client.get(f'{path}?q={blank}')
                self.assertEqual(400, response.status_code, blank)
                self.assertEqual('knowledge.invalid_query', response.json()['code'])
            self.search.assert_not_awaited()
            # Exactly the maximum length is still accepted and reaches the search.
            self.assertEqual(200, (await client.get(f'{path}?q=' + 'x' * 500)).status_code)
        self.assertEqual(1, self.search.await_count)

    async def test_bad_scope_is_404_and_never_reaches_the_search(self) -> None:
        mine = await self.h.repo.create_knowledge_base(1, 'Mine')
        other = await self.h.repo.create_knowledge_base(2, 'Theirs')
        # Persist the fixture: every 404 below makes the route's transaction roll back.
        self.h.raw.commit()
        async with self.acting_as(self.alice) as client:
            response = await client.get(f'/api/knowledge-base/{other["id"]}/search?q=t')
            self.assertEqual(404, response.status_code)
            self.assertEqual('knowledge_base.not_found', response.json()['code'])
            for bad in ('not-a-uuid', str(uuid4())):
                response = await client.get(f'/api/knowledge-base/{bad}/search?q=t')
                self.assertEqual(404, response.status_code, bad)
                self.assertEqual('knowledge_base.not_found', response.json()['code'])
        async with self.acting_as(self.bob) as client:
            self.assertEqual(404, (await client.get(f'/api/knowledge-base/{mine["id"]}/search?q=t')).status_code)
        async with self.acting_as(self.admin) as client:
            # An admin has no cross-user privilege on a private knowledge base.
            self.assertEqual(404, (await client.get(f'/api/knowledge-base/{mine["id"]}/search?q=t')).status_code)
        self.assertTrue(await self.h.repo.delete_knowledge_base(1, mine['id']))
        async with self.acting_as(self.alice) as client:
            self.assertEqual(404, (await client.get(f'/api/knowledge-base/{mine["id"]}/search?q=t')).status_code)
        self.search.assert_not_awaited()

    async def test_explicit_document_must_be_owned_in_this_base_and_ready(self) -> None:
        mine = await self.h.repo.create_knowledge_base(1, 'Mine')
        sibling = await self.h.repo.create_knowledge_base(1, 'Sibling')
        ready = await self.h.publish(base=mine, chunks=[Chunk(1, 0, 'alpha')])
        pending = await self.h.repo.create_document(1, mine['id'], filename='p.pdf')
        elsewhere = await self.h.repo.create_document(1, sibling['id'], filename='s.pdf')
        foreign = await self.h.publish(owner=2, chunks=[Chunk(1, 0, 'beta')])
        # Persist the fixture: every 404/409 below makes the route's transaction roll back.
        self.h.raw.commit()

        async with self.acting_as(self.alice) as client:
            path = f'/api/knowledge-base/{mine["id"]}/search'
            # Unknown, malformed, foreign and not-in-this-base documents are 404.
            # ``elsewhere`` is a raw ``create_document`` row while ``foreign`` is a
            # publish bundle; each carries its id under its own key.
            for bad in (str(uuid4()), 'not-a-uuid', elsewhere['id'], foreign['doc']['id']):
                response = await client.get(f'{path}?q=t&document_id={bad}')
                self.assertEqual(404, response.status_code, bad)
                self.assertEqual('document.not_found', response.json()['code'])
            # An owned document of this base that has not been published is a 409.
            not_ready = await client.get(f'{path}?q=t&document_id={pending["id"]}')
            self.assertEqual(409, not_ready.status_code)
            self.assertEqual('document.not_ready', not_ready.json()['code'])
            self.search.assert_not_awaited()

            # The ready document is passed on by its canonical id.
            self.search.return_value = [self._row(mine['id'], ready['doc']['id'])]
            accepted = await client.get(f'{path}?q=t&document_id={ready["doc"]["id"]}')
            self.assertEqual(200, accepted.status_code)
        self.search.assert_awaited_once_with(
            1,
            't',
            knowledge_base_id=mine['id'],
            document_id=ready['doc']['id'],
            limit=21,
            offset=0,
        )

    async def test_search_passes_canonical_scope_a_trimmed_query_and_the_probe(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Mine')
        self.h.raw.commit()
        async with self.acting_as(self.alice) as client:
            self.search.return_value = []
            response = await client.get(f'/api/knowledge-base/{base["id"]}/search?q=%20%20term%20%20&limit=7&offset=9')
            self.assertEqual(200, response.status_code)
            self.assertEqual({'hits': [], 'next_offset': None}, response.json())
            # The default page and the cap: the repository is asked for limit + 1.
            default = await client.get(f'/api/knowledge-base/{base["id"]}/search?q=t')
            self.assertEqual(200, default.status_code)
            max_page = await client.get(f'/api/knowledge-base/{base["id"]}/search?q=t&limit=100')
            self.assertEqual(200, max_page.status_code)
        calls = self.search.await_args_list
        self.assertEqual(calls[0].args, (1, 'term'))
        self.assertEqual(
            calls[0].kwargs,
            {'knowledge_base_id': base['id'], 'document_id': None, 'limit': 8, 'offset': 9},
        )
        self.assertEqual(calls[1].args, (1, 't'))
        self.assertEqual(
            calls[1].kwargs, {'knowledge_base_id': base['id'], 'document_id': None, 'limit': 21, 'offset': 0}
        )
        # limit=100 is the cap, so the probe stays at limit + 1 (101 <= repo bound 200).
        self.assertEqual(
            calls[2].kwargs, {'knowledge_base_id': base['id'], 'document_id': None, 'limit': 101, 'offset': 0}
        )

    async def test_pagination_is_bounded_and_next_offset_is_exact(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Mine')
        self.h.raw.commit()
        doc_id = str(uuid4())
        async with self.acting_as(self.alice) as client:
            path = f'/api/knowledge-base/{base["id"]}/search'
            for query in ('limit=0', 'limit=101', 'limit=-1', 'offset=-1', 'limit=none'):
                self.assertEqual(422, (await client.get(f'{path}?q=t&{query}')).status_code, query)

            # A page with a further row: the probe row is dropped and counted.
            self.search.return_value = [self._row(base['id'], doc_id, ordinal=index) for index in range(11)]
            more = await client.get(f'{path}?q=t&limit=10&offset=40')
            self.assertEqual(10, len(more.json()['hits']))
            self.assertEqual(50, more.json()['next_offset'])

            # A page that is exactly full must not claim a successor.
            self.search.return_value = [self._row(base['id'], doc_id, ordinal=index) for index in range(10)]
            full = await client.get(f'{path}?q=t&limit=10')
            self.assertEqual(10, len(full.json()['hits']))
            self.assertIsNone(full.json()['next_offset'])

            # The default page is 20 rows and reports 20 when 21 come back.
            self.search.return_value = [self._row(base['id'], doc_id, ordinal=index) for index in range(21)]
            default = await client.get(f'{path}?q=t')
            self.assertEqual(20, len(default.json()['hits']))
            self.assertEqual(20, default.json()['next_offset'])

    async def test_hits_use_the_public_whitelist_and_keep_verbatim_text(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Mine')
        self.h.raw.commit()
        doc_id = str(uuid4())
        text = '<script>alert("x")</script> & \u4e2d\u6587\u6d4b\u8bd5 \U0001f600'
        row = self._row(base['id'], doc_id, text=text, chunk_type='heading', heading_level=2)
        self.search.return_value = [row]
        async with self.acting_as(self.alice) as client:
            response = await client.get(f'/api/knowledge-base/{base["id"]}/search?q=t')
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(['hits', 'next_offset'], list(body))
        self.assertIsNone(body['next_offset'])
        self.assertEqual(
            [
                {
                    'document_id': doc_id,
                    'knowledge_base_id': base['id'],
                    'filename': 'a.pdf',
                    'version': 2,
                    'page_no': 1,
                    'ordinal': 0,
                    'text': text,
                    'chunk_type': 'heading',
                    'heading_level': 2,
                }
            ],
            body['hits'],
        )
        self.assertEqual(PUBLIC_SEARCH_HIT_FIELDS, tuple(body['hits'][0]))
        # The text is plain UTF-8, not double-escaped HTML, and is never parsed.
        self.assertIn('<script>', response.text)
        self.assertNotIn('&lt;script&gt;', response.text)
        self.assertNotIn('\\u003cscript', response.text)
        # Every internal column of the row stayed server-side.
        for secret in (*SERVER_OWNED_KEYS, *SERVER_OWNED_VALUES, str(row['lease_token'])):
            self.assertNotIn(secret, response.text)

    async def test_every_paging_entry_bounds_offset_to_the_signed_int8(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Mine')
        published = await self.h.publish(base=base, chunks=[Chunk(1, 0, 'alpha')])
        doc_id = published['doc']['id']
        self.h.raw.commit()
        too_big = MAX_OFFSET + 1
        paths = (
            '/api/knowledge-base',
            f'/api/knowledge-base/{base["id"]}/document',
            f'/api/document/{doc_id}/content',
            f'/api/knowledge-base/{base["id"]}/search?q=t',
        )
        async with self.acting_as(self.alice) as client:
            # Over the signed int8 bound PostgreSQL OFFSET accepts: 422, and the
            # search endpoint never even reaches the (mocked) full-text call.
            for path in paths:
                joiner = '&' if '?' in path else '?'
                response = await client.get(f'{path}{joiner}offset={too_big}')
                self.assertEqual(422, response.status_code, path)
            self.search.assert_not_awaited()

            # Exactly the bound is accepted: a real (small) SQLite table answers an
            # empty page, and the search passes the bound on to the repository.
            self.search.return_value = []
            for path in paths:
                joiner = '&' if '?' in path else '?'
                response = await client.get(f'{path}{joiner}offset={MAX_OFFSET}')
                self.assertEqual(200, response.status_code, path)
                if 'search' in path:
                    self.assertEqual({'hits': [], 'next_offset': None}, response.json())
                elif 'content' in path:
                    self.assertEqual([], response.json()['chunks'])
        self.search.assert_awaited_once_with(
            1,
            't',
            knowledge_base_id=base['id'],
            document_id=None,
            limit=21,
            offset=MAX_OFFSET,
        )

    def test_search_sql_returns_the_stored_text_without_a_headline(self) -> None:
        # ``ts_headline`` would let PostgreSQL wrap matched terms in markup, so the
        # route must select the stored chunk text itself; verbatim output is
        # asserted by the whitelist test above.
        from hippo.repositories.knowledge import chunk_search_query

        sql, _ = chunk_search_query(1, 'term', knowledge_base_id='kb-1')
        self.assertNotIn('ts_headline', sql)
        self.assertIn('c.text', sql)


if __name__ == '__main__':
    unittest.main()
