"""Owner-scoped behaviour of ``KnowledgeRepository``.

Most tests execute real SQL against an in-memory SQLite database through a thin
async adapter, so the CRUD and ownership queries are actually parsed and run
rather than asserted against a mock that already knows the right answer.

What SQLite can and cannot cover:

* It covers ``knowledge_base``/``document``/``document_chunk`` CRUD, ownership
  joins, every CHECK constraint, tombstones and the partial-index behaviour, all
  of which are portable SQL.
* For the upload state machine it also covers the ``SAVEPOINT`` around
  ``finish_upload`` and the live-SHA unique index: the SQLite double translates
  only the conflict that one index raises on that one statement into psycopg's
  ``UniqueViolation`` with the matching constraint diagnostic, so the
  ``duplicate`` branch is exercised by a real constraint failure. Every other
  integrity error keeps its own type.
* It cannot execute PostgreSQL session settings either: the two fixed
  ``SET LOCAL`` statements the two maintenance methods issue are recorded and
  no-op'd, and only those exact two, so no other SQL is ever ignored. Whether
  the server really applies the 5s/10s values is not verified by this harness.
* It cannot model row locks, so the PostgreSQL ``FOR SHARE`` ordering in
  ``create_document``/``delete_knowledge_base`` is asserted statically and from
  captured SQL text, never by real concurrency.
* It does **not** cover PostgreSQL-only pieces: the ``jiebaqry`` dictionary, the
  generated ``tsvector`` column, the GIN index and ``search_chunks``'s
  ``plainto_tsquery``/``ts_rank`` query. Those are only checked statically here
  and are explicitly marked as *not executed against a real PostgreSQL server*.
"""

from __future__ import annotations

import hashlib
import inspect
import re
import sqlite3
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg

from hippo.knowledge.parse import Chunk
from hippo.repositories import KnowledgeRepository
from hippo.repositories.knowledge import (
    ACTIVE_SHA_CONSTRAINT,
    DEFAULT_PAGE_SIZE,
    FINISH_SAVEPOINT,
    IMPORT_CHUNK_BATCH_SIZE,
    MAX_CHUNK_CHARS,
    MAX_CHUNKS,
    MAX_ERROR_CODE_LENGTH,
    MAX_ERROR_MESSAGE_LENGTH,
    MAX_FILENAME_LENGTH,
    MAX_NAME_LENGTH,
    MAX_OFFSET,
    MAX_PAGE_SIZE,
    MAX_PDF_PAGES,
    MAX_QUERY_LENGTH,
    MAX_TEXT_CHARS,
    MAX_WORKER_ID_LENGTH,
    UPLOAD_LOCK_OWNER,
    chunk_search_query,
)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / 'schema' / 'postgres.sql'

# SQLite has no row locks and no PostgreSQL-only CTE materialization. Only those
# two syntax markers are removed so the surrounding owner/active WHERE clauses
# still run for real; that the source SQL asks for the right lock is asserted
# separately, from the captured text, in `DocumentOwnershipTest` and `LockingSqlTest`.
_PG_ONLY_LOCKS = re.compile(r'^\s*FOR SHARE(?: OF kb)?\s*$', re.MULTILINE)
_PG_ONLY_MATERIALIZED = re.compile(r'\bAS MATERIALIZED\b')

#: The finishing UPDATE is the only statement whose live-SHA conflict is a
#: dedupe answer; the same conflict from any other statement (for instance a
#: ``create_document`` with a duplicate hash) must keep its original type.
_FINISH_UPDATE_MARKER = "SET state = 'queued'"
_LIVE_SHA_CONFLICT = re.compile(r'UNIQUE constraint failed: .*(?:document\.sha256|idx_document_active_sha256)')

#: The two fixed server-side limits the maintenance methods set, in the order they
#: are issued. SQLite has no session settings, so the harness records and no-ops
#: exactly these two statements and nothing else: any other ``SET`` really runs
#: (and fails) in SQLite instead of being silently ignored.
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
STATEMENT_TIMEOUT_SQL = "SET LOCAL statement_timeout = '10s'"
_SERVER_ONLY_STATEMENTS = frozenset({LOCK_TIMEOUT_SQL, STATEMENT_TIMEOUT_SQL})


class _ConstraintDiagnostic:
    """The single diagnostic field ``finish_upload`` reads, without a server row."""

    def __init__(self, constraint_name: str) -> None:
        self.constraint_name = constraint_name


class _SqliteShaUniqueViolation(psycopg.errors.UniqueViolation):
    """A real ``UniqueViolation`` carrying the live-SHA constraint diagnostic.

    The SQLite double raises this for a conflict on ``idx_document_active_sha256``
    so the repository's ``except`` clause and constraint check run for real;
    only the diagnostics object is supplied locally, because psycopg normally
    fills it from a server result the adapter never sees. ``diag`` is overridden
    as a property with a write-ignoring setter, so the class works whether
    psycopg's own ``__init__`` assigns a diagnostics object or not. No
    ``__slots__`` here: a Python exception subclass already has a ``__dict__``,
    which the base class may lack.
    """

    def __init__(self, message: str, constraint_name: str) -> None:
        super().__init__(message)
        self._sqlite_constraint_name = constraint_name

    @property
    def diag(self) -> _ConstraintDiagnostic:
        return _ConstraintDiagnostic(self._sqlite_constraint_name)

    @diag.setter
    def diag(self, value: object) -> None:
        pass


# Dates reach SQLite as ISO text: Python 3.12 deprecated sqlite3's implicit
# datetime adapter, and the tests never need the values back as datetimes.
#
# Portable subset of the PostgreSQL knowledge tables: identities, ownership and
# every CHECK constraint are the same; the tsvector column, its GIN index and the
# `jiebaqry` dictionary cannot exist in SQLite and are omitted.
_SCHEMA_SQL = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE
);

CREATE TABLE knowledge_base (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    deleted_at TIMESTAMPTZ
);

CREATE TABLE document (
    id TEXT PRIMARY KEY,
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_base(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    mime_type TEXT,
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    sha256 TEXT,
    s3_key TEXT,
    state TEXT NOT NULL DEFAULT 'pending_upload' CHECK (
        state IN ('pending_upload', 'uploading', 'queued', 'processing',
                  'ready', 'failed', 'unsupported', 'deleted')
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    error_code TEXT,
    error_message TEXT,
    page_count INTEGER CHECK (page_count IS NULL OR page_count >= 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    locked_by TEXT,
    lease_token TEXT,
    locked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    deleted_at TIMESTAMPTZ,
    CHECK ((deleted_at IS NOT NULL) = (state = 'deleted'))
);

CREATE UNIQUE INDEX idx_document_active_sha256
ON document (knowledge_base_id, sha256)
WHERE sha256 IS NOT NULL AND deleted_at IS NULL;

CREATE INDEX idx_document_queued_import
ON document (created_at, id)
WHERE state = 'queued' AND deleted_at IS NULL;

CREATE TABLE document_chunk (
    document_id TEXT NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL CHECK (page_no >= 1),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    text TEXT NOT NULL,
    chunk_type TEXT NOT NULL DEFAULT 'paragraph',
    heading_level INTEGER,
    PRIMARY KEY (document_id, page_no, ordinal)
);

-- Mirrors the existing personal table this component must never touch.
CREATE TABLE annotation (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    article_pk INTEGER NOT NULL,
    quote TEXT NOT NULL
);
"""


class _AsyncCursor:
    """Minimal async context manager over a sqlite3 cursor.

    Rows are always returned as dicts (column name -> value) to match psycopg's
    ``dict_row``; the repository only reads rows by column name. Executed SQL is
    recorded so tests can assert on the owner filters that actually ran.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        executed: list[str],
        *,
        sha_constraint: str,
        savepoint_faults: list[str] | None = None,
        executemany_faults: list[int] | None = None,
    ) -> None:
        self._cur = conn.cursor()
        self._executed = executed
        self._sha_constraint = sha_constraint
        # One-shot statements the fake server must reject, consumed as they are hit.
        self._savepoint_faults = savepoint_faults if savepoint_faults is not None else []
        # Counts down the real ``executemany`` calls that must still succeed before
        # one is rejected, so a batched insert can fail part-way through.
        self._executemany_faults = executemany_faults if executemany_faults is not None else []
        # ``WITH ... UPDATE`` changes rows but SQLite reports ``rowcount`` as -1;
        # the real delta is kept here for that statement shape only.
        self._with_dml_rowcount: int | None = None

    async def __aenter__(self) -> _AsyncCursor:
        return self

    async def __aexit__(self, *exc_info) -> bool:
        self._cur.close()
        return False

    async def execute(self, sql: str, params: object = ()) -> _AsyncCursor:
        values = tuple(value.isoformat() if isinstance(value, datetime) else value for value in (params or ()))
        self._executed.append(sql)
        self._with_dml_rowcount = None
        if sql.strip() in _SERVER_ONLY_STATEMENTS:
            # Recorded for the ordering assertions, never sent to SQLite.
            return self
        portable = _PG_ONLY_MATERIALIZED.sub('AS', _PG_ONLY_LOCKS.sub('', sql))
        fault = self._take_savepoint_fault(portable)
        if fault is not None:
            # The cleanup statement was issued but the fake server rejected it, so
            # the adapter does not run it and the caller must see the failure.
            raise RuntimeError(f'simulated savepoint cleanup failure: {fault}')
        changes_before = self._cur.connection.total_changes
        try:
            self._cur.execute(portable.replace('%s', '?'), values)
        except sqlite3.IntegrityError as exc:
            if _FINISH_UPDATE_MARKER in portable and _LIVE_SHA_CONFLICT.search(str(exc)):
                raise _SqliteShaUniqueViolation(str(exc), self._sha_constraint) from exc
            raise
        if portable.lstrip().upper().startswith('WITH') and self._cur.description is None:
            self._with_dml_rowcount = self._cur.connection.total_changes - changes_before
        return self

    def _take_savepoint_fault(self, portable: str) -> str | None:
        """Consume a configured fault for one savepoint cleanup statement.

        ``rollback`` targets ``ROLLBACK TO SAVEPOINT`` and ``release`` targets
        ``RELEASE SAVEPOINT``; either may be armed alone so the test can pick
        which half of the cleanup fails.
        """
        for kind in ('rollback', 'release'):
            if kind in self._savepoint_faults and kind.upper() in portable.upper():
                self._savepoint_faults.remove(kind)
                return kind
        return None

    async def executemany(self, sql: str, params_seq: object) -> None:
        """The real batched insert, recorded so tests can count the batches.

        ``psycopg``'s ``executemany`` is what the repository must use for chunks:
        one round trip per bounded batch instead of one per row.
        """
        self._executed.append(sql)
        self._with_dml_rowcount = None
        rows = [
            tuple(value.isoformat() if isinstance(value, datetime) else value for value in row)
            for row in params_seq  # type: ignore[union-attr]
        ]
        fault = self._take_executemany_fault()
        if fault is not None:
            raise RuntimeError(f'simulated chunk insert failure: {fault}')
        self._cur.executemany(sql.replace('%s', '?'), rows)

    def _take_executemany_fault(self) -> int | None:
        """Consume a configured batch failure, counting down the allowed batches."""
        if not self._executemany_faults:
            return None
        remaining = self._executemany_faults[0]
        if remaining <= 0:
            self._executemany_faults.pop(0)
            return remaining
        self._executemany_faults[0] = remaining - 1
        return None

    async def fetchone(self) -> dict | None:
        row = self._cur.fetchone()
        return None if row is None else self._shape(row)

    async def fetchall(self) -> list[dict]:
        return [self._shape(row) for row in self._cur.fetchall()]

    @property
    def rowcount(self) -> int:
        # PostgreSQL reports the real count for ``WITH ... UPDATE``; SQLite answers
        # -1, so the adapter supplies the measured delta. Only a CTE statement with
        # no result set is affected - a plain DML statement or a SELECT keeps the
        # native count, and a rejected statement records nothing.
        native = self._cur.rowcount
        if native == -1 and self._with_dml_rowcount is not None:
            return self._with_dml_rowcount
        return native

    def _shape(self, row: tuple) -> dict:
        columns = [description[0] for description in self._cur.description]
        return dict(zip(columns, row, strict=True))


class _AsyncConnection:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        sha_constraint: str = ACTIVE_SHA_CONSTRAINT,
    ) -> None:
        self._conn = conn
        self._sha_constraint = sha_constraint
        # Shared across cursors; the harness appends to it to fail one cleanup step.
        self._savepoint_faults: list[str] = []
        # Shared across cursors; the harness arms it to fail one batched insert.
        self._executemany_faults: list[int] = []
        # Every statement the repository ran, in execution order.
        self.executed: list[str] = []

    def cursor(self, row_factory=None) -> _AsyncCursor:
        return _AsyncCursor(
            self._conn,
            self.executed,
            sha_constraint=self._sha_constraint,
            savepoint_faults=self._savepoint_faults,
            executemany_faults=self._executemany_faults,
        )


class _Harness:
    """In-memory SQLite database plus the repository under test."""

    def __init__(self, *, sha_constraint: str = ACTIVE_SHA_CONSTRAINT) -> None:
        self.raw = sqlite3.connect(':memory:')
        self.raw.execute('PRAGMA foreign_keys = ON')
        self.raw.executescript(_SCHEMA_SQL)
        self.raw.executemany('INSERT INTO users (id, username) VALUES (?, ?)', [(1, 'alice'), (2, 'bob')])
        self.raw.commit()
        self.connection = _AsyncConnection(self.raw, sha_constraint=sha_constraint)
        self.repo = KnowledgeRepository(self.connection)  # type: ignore[arg-type]

    @property
    def executed(self) -> list[str]:
        return self.connection.executed

    def seed_chunk(
        self,
        document_id: str,
        page_no: int,
        ordinal: int,
        text: str,
        *,
        chunk_type: str = 'paragraph',
        heading_level: int | None = None,
    ) -> None:
        self.raw.execute(
            'INSERT INTO document_chunk (document_id, page_no, ordinal, text, chunk_type, heading_level)'
            ' VALUES (?, ?, ?, ?, ?, ?)',
            (document_id, page_no, ordinal, text, chunk_type, heading_level),
        )
        self.raw.commit()

    def document_row(self, document_id: str) -> sqlite3.Row | None:
        self.raw.row_factory = sqlite3.Row
        try:
            return self.raw.execute('SELECT * FROM document WHERE id = ?', (document_id,)).fetchone()
        finally:
            self.raw.row_factory = None

    async def outer_transaction(self) -> None:
        """What ``StorageTransaction`` does when the caller's block raises."""
        self.raw.rollback()

    def fail_next_savepoint_cleanup(self, action: str) -> None:
        """Make the next ``ROLLBACK TO``/``RELEASE`` savepoint statement fail."""
        assert action in ('rollback', 'release')
        self.connection._savepoint_faults.append(action)

    def fail_chunk_insert_after(self, batches: int) -> None:
        """Make the ``batches + 1``-th following ``executemany`` call fail."""
        assert batches >= 0
        self.connection._executemany_faults.append(batches)

    def count(self, sql: str, params: tuple = ()) -> int:
        return int(self.raw.execute(sql, params).fetchone()[0])


class KnowledgeBaseOwnershipTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.h = _Harness()

    def tearDown(self) -> None:
        self.h.raw.close()

    async def test_knowledge_base_crud_is_owner_scoped(self) -> None:
        created = await self.h.repo.create_knowledge_base(1, 'Papers')
        kb_id = created['id']
        self.assertEqual(created['user_id'], 1)
        self.assertIsNone(created['deleted_at'])

        self.assertEqual([row['id'] for row in await self.h.repo.list_knowledge_bases(1)], [kb_id])
        # Another user sees nothing, including through the returned id.
        self.assertEqual(await self.h.repo.list_knowledge_bases(2), [])
        self.assertIsNone(await self.h.repo.get_knowledge_base(2, kb_id))

        self.assertFalse(await self.h.repo.rename_knowledge_base(2, kb_id, 'Stolen'))
        self.assertEqual((await self.h.repo.get_knowledge_base(1, kb_id))['name'], 'Papers')
        self.assertTrue(await self.h.repo.rename_knowledge_base(1, kb_id, 'Reading'))
        self.assertEqual((await self.h.repo.get_knowledge_base(1, kb_id))['name'], 'Reading')

    async def test_unknown_and_malformed_ids_are_misses(self) -> None:
        self.assertIsNone(await self.h.repo.get_knowledge_base(1, str(uuid4())))
        self.assertIsNone(await self.h.repo.get_knowledge_base(1, 'not-a-uuid'))
        self.assertIsNone(await self.h.repo.get_knowledge_base(1, None))
        self.assertFalse(await self.h.repo.rename_knowledge_base(1, 'not-a-uuid', 'x'))
        self.assertFalse(await self.h.repo.delete_knowledge_base(1, 'not-a-uuid'))

    async def test_names_and_lengths_are_validated(self) -> None:
        for bad in ('', '   ', None, 123, b'bytes', ['x']):
            with self.assertRaises(ValueError, msg=f'accepted {bad!r}'):
                await self.h.repo.create_knowledge_base(1, bad)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            await self.h.repo.create_knowledge_base(1, 'x' * (MAX_NAME_LENGTH + 1))
        base = await self.h.repo.create_knowledge_base(1, 'ok')
        with self.assertRaises(ValueError):
            await self.h.repo.create_document(1, base['id'], filename='y' * (MAX_FILENAME_LENGTH + 1))
        with self.assertRaises(ValueError):
            await self.h.repo.create_document(1, base['id'], filename='   ')
        with self.assertRaises(ValueError):
            await self.h.repo.create_document(1, base['id'], filename=42)  # type: ignore[arg-type]

    async def test_delete_knowledge_base_tombstones_docs_and_keeps_personal_data(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Papers')
        doc = await self.h.repo.create_document(
            1,
            base['id'],
            filename='a.pdf',
            mime_type='application/pdf',
            size_bytes=12,
            s3_key='kb/a.pdf',
        )
        self.h.seed_chunk(doc['id'], 1, 0, 'hello world')
        self.h.raw.execute(
            'INSERT INTO annotation (id, user_id, article_pk, quote) VALUES (?, ?, ?, ?)',
            (7, 1, 42, 'q'),
        )
        self.h.raw.commit()

        self.assertFalse(await self.h.repo.delete_knowledge_base(2, base['id']))
        self.assertIsNotNone(await self.h.repo.get_knowledge_base(1, base['id']))
        # Deleting somebody else's base leaves its documents live.
        self.assertIsNone(self.h.document_row(doc['id'])['deleted_at'])

        self.assertTrue(await self.h.repo.delete_knowledge_base(1, base['id']))
        self.assertIsNone(await self.h.repo.get_knowledge_base(1, base['id']))
        # A second delete is a no-op miss, not another sweep.
        self.assertFalse(await self.h.repo.delete_knowledge_base(1, base['id']))
        self.assertEqual(self.h.count('SELECT COUNT(*) FROM document_chunk'), 0)
        # The document row survives as a tombstone; the stored object is not deleted here.
        row = self.h.document_row(doc['id'])
        self.assertIsNotNone(row)
        self.assertEqual(row['state'], 'deleted')
        self.assertIsNotNone(row['deleted_at'])
        self.assertEqual(row['s3_key'], 'kb/a.pdf')
        # Existing personal data is untouched.
        self.assertEqual(self.h.count('SELECT COUNT(*) FROM annotation WHERE id = 7'), 1)


class DocumentOwnershipTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.h = _Harness()

    def tearDown(self) -> None:
        self.h.raw.close()

    async def test_create_document_requires_own_live_base(self) -> None:
        mine = await self.h.repo.create_knowledge_base(1, 'Mine')
        theirs = await self.h.repo.create_knowledge_base(2, 'Theirs')

        self.assertIsNone(await self.h.repo.create_document(1, theirs['id'], filename='x.pdf'))
        self.assertIsNone(await self.h.repo.create_document(2, mine['id'], filename='x.pdf'))
        self.assertIsNone(await self.h.repo.create_document(1, str(uuid4()), filename='x.pdf'))
        self.assertIsNone(await self.h.repo.create_document(1, 'not-a-uuid', filename='x.pdf'))
        self.assertEqual(self.h.count('SELECT COUNT(*) FROM document'), 0)

        created = await self.h.repo.create_document(1, mine['id'], filename='x.pdf', mime_type='application/pdf')
        self.assertEqual(created['knowledge_base_id'], mine['id'])
        self.assertEqual(created['state'], 'pending_upload')
        self.assertEqual(created['attempts'], 0)
        self.assertIsNone(created['sha256'])

        await self.h.repo.delete_knowledge_base(1, mine['id'])
        self.assertIsNone(await self.h.repo.create_document(1, mine['id'], filename='after-delete.pdf'))

    async def test_documents_do_not_leak_across_users(self) -> None:
        mine = await self.h.repo.create_knowledge_base(1, 'Mine')
        doc = await self.h.repo.create_document(1, mine['id'], filename='x.pdf')

        self.assertIsNone(await self.h.repo.get_document(2, doc['id']))
        self.assertEqual(await self.h.repo.list_documents(2, mine['id']), [])
        self.assertFalse(await self.h.repo.delete_document(2, doc['id']))

        self.assertEqual((await self.h.repo.get_document(1, doc['id']))['filename'], 'x.pdf')
        self.assertEqual(len(await self.h.repo.list_documents(1, mine['id'])), 1)

    async def test_delete_document_hides_content_and_keeps_tombstone(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Mine')
        doc = await self.h.repo.create_document(1, base['id'], filename='x.pdf', s3_key='kb/x.pdf')
        other = await self.h.repo.create_document(1, base['id'], filename='other.pdf')
        self.h.seed_chunk(doc['id'], 1, 0, 'first')
        self.h.seed_chunk(doc['id'], 2, 0, 'second')
        self.h.seed_chunk(other['id'], 1, 0, 'unrelated')
        # A worker holds a lease on the document; the tombstone must void it.
        self.h.raw.execute(
            'UPDATE document SET locked_by = ?, lease_token = ?, locked_at = ? WHERE id = ?',
            ('worker-1', str(uuid4()), '2020-01-01T00:00:00+00:00', doc['id']),
        )
        self.h.raw.commit()

        self.assertEqual(await self.h.repo.list_chunks(2, doc['id']), [])
        pages = await self.h.repo.list_chunks(1, doc['id'])
        self.assertEqual([(row['page_no'], row['ordinal']) for row in pages], [(1, 0), (2, 0)])

        self.assertTrue(await self.h.repo.delete_document(1, doc['id']))
        self.assertIsNone(await self.h.repo.get_document(1, doc['id']))
        self.assertEqual(await self.h.repo.list_chunks(1, doc['id']), [])
        # Only the tombstoned document leaves the list; the sibling stays live.
        listed = await self.h.repo.list_documents(1, base['id'])
        self.assertEqual([item['id'] for item in listed], [other['id']])
        self.assertNotIn(doc['id'], [item['id'] for item in listed])
        # Repeating the delete is a miss, never a second sweep.
        self.assertFalse(await self.h.repo.delete_document(1, doc['id']))
        row = self.h.document_row(doc['id'])
        self.assertEqual(row['state'], 'deleted')
        self.assertEqual(row['s3_key'], 'kb/x.pdf')
        self.assertIsNone(row['locked_by'])
        self.assertIsNone(row['lease_token'])
        self.assertIsNone(row['locked_at'])
        # A sibling document in the same base keeps its chunks.
        self.assertEqual(len(await self.h.repo.list_chunks(1, other['id'])), 1)
        self.assertIsNone(self.h.document_row(other['id'])['deleted_at'])

    async def test_document_state_and_numeric_checks(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Mine')
        doc = await self.h.repo.create_document(1, base['id'], filename='x.pdf')

        # Defaults are the legal starting point for an upload.
        row = self.h.document_row(doc['id'])
        self.assertEqual(row['state'], 'pending_upload')
        self.assertEqual(row['attempts'], 0)
        self.assertEqual(row['version'], 1)
        self.assertIsNone(row['size_bytes'])
        self.assertIsNone(row['page_count'])

        def rejected(sql: str, params: tuple) -> None:
            with self.assertRaises(sqlite3.IntegrityError):
                self.h.raw.execute(sql, params)

        rejected('UPDATE document SET state = ? WHERE id = ?', ('archived', doc['id']))
        rejected('UPDATE document SET attempts = ? WHERE id = ?', (-1, doc['id']))
        rejected('UPDATE document SET version = ? WHERE id = ?', (0, doc['id']))
        rejected('UPDATE document SET size_bytes = ? WHERE id = ?', (-1, doc['id']))
        rejected('UPDATE document SET page_count = ? WHERE id = ?', (-1, doc['id']))
        # A tombstone needs both halves, and neither half alone is accepted.
        rejected('UPDATE document SET state = ? WHERE id = ?', ('deleted', doc['id']))
        rejected('UPDATE document SET deleted_at = ? WHERE id = ?', ('2020-01-01T00:00:00+00:00', doc['id']))

        # Every other legal state still passes, with metadata left unset.
        for state in ('uploading', 'queued', 'processing', 'ready', 'failed', 'unsupported'):
            self.h.raw.execute(
                'UPDATE document SET state = ?, size_bytes = NULL, page_count = NULL WHERE id = ?',
                (state, doc['id']),
            )
            self.h.raw.commit()
            self.assertEqual(self.h.document_row(doc['id'])['state'], state)

    async def test_create_document_locks_the_parent_row(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Mine')
        start = len(self.h.executed)
        self.assertIsNotNone(await self.h.repo.create_document(1, base['id'], filename='x.pdf'))
        insert = next(sql for sql in self.h.executed[start:] if 'INSERT INTO document' in sql)
        # FOR KEY SHARE would not conflict with the delete's non-key update, so the
        # shared lock must be requested explicitly, on a materialized CTE whose
        # predicate already carries the owner and liveness checks.
        self.assertIn('AS MATERIALIZED', insert)
        self.assertIn('FOR SHARE', insert)
        self.assertNotIn('FOR KEY SHARE', insert)
        self.assertIn('kb.user_id = %s', insert)
        self.assertIn('kb.deleted_at IS NULL', insert)

    async def test_duplicate_pages_and_text_are_allowed(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Mine')
        doc = await self.h.repo.create_document(1, base['id'], filename='x.pdf', page_count=3)

        # Same text on different pages, the same text twice on one page, and a blank page
        # (an empty text chunk; a page may also have no chunks at all).
        self.h.seed_chunk(doc['id'], 1, 0, 'repeated')
        self.h.seed_chunk(doc['id'], 1, 1, 'repeated')
        self.h.seed_chunk(doc['id'], 2, 0, 'repeated')
        self.h.seed_chunk(doc['id'], 3, 0, '')
        chunks = await self.h.repo.list_chunks(1, doc['id'])
        self.assertEqual(
            [(row['page_no'], row['ordinal']) for row in chunks],
            [(1, 0), (1, 1), (2, 0), (3, 0)],
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.h.seed_chunk(doc['id'], 1, 0, 'conflicting ordinal')
        # Page numbers are 1-based and ordinals are non-negative.
        with self.assertRaises(sqlite3.IntegrityError):
            self.h.seed_chunk(doc['id'], 0, 0, 'page zero')
        with self.assertRaises(sqlite3.IntegrityError):
            self.h.seed_chunk(doc['id'], 1, -1, 'negative ordinal')

    async def test_live_sha256_is_unique_while_null_and_deleted_do_not_block(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Mine')
        await self.h.repo.create_document(1, base['id'], filename='a.pdf', sha256='abc')
        # Rows whose content hash is not known yet (still uploading) never conflict.
        await self.h.repo.create_document(1, base['id'], filename='a2.pdf')
        await self.h.repo.create_document(1, base['id'], filename='a3.pdf')
        with self.assertRaises(sqlite3.IntegrityError):
            await self.h.repo.create_document(1, base['id'], filename='dup.pdf', sha256='abc')

        # Deleting the first copy lets the same bytes be uploaded again.
        doc_id = self.h.raw.execute("SELECT id FROM document WHERE sha256 = 'abc'").fetchone()[0]
        self.assertTrue(await self.h.repo.delete_document(1, doc_id))
        again = await self.h.repo.create_document(1, base['id'], filename='again.pdf', sha256='abc')
        self.assertIsNotNone(again)
        # Another base can hold the same bytes; the index is per knowledge base.
        other = await self.h.repo.create_knowledge_base(1, 'Other')
        self.assertIsNotNone(await self.h.repo.create_document(1, other['id'], filename='a.pdf', sha256='abc'))

    async def test_pagination_is_bounded_and_offset_safe(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Mine')
        for index in range(MAX_PAGE_SIZE + 5):
            await self.h.repo.create_document(1, base['id'], filename=f'{index}.pdf')

        self.assertEqual(len(await self.h.repo.list_documents(1, base['id'], limit=10**9)), MAX_PAGE_SIZE)
        # A non-positive or unparseable page size falls back to the default page.
        self.assertEqual(len(await self.h.repo.list_documents(1, base['id'], limit=0)), DEFAULT_PAGE_SIZE)
        self.assertEqual(len(await self.h.repo.list_documents(1, base['id'], limit=-5, offset=-5)), DEFAULT_PAGE_SIZE)
        bogus = await self.h.repo.list_documents(1, base['id'], limit='nope')  # type: ignore[arg-type]
        self.assertEqual(len(bogus), DEFAULT_PAGE_SIZE)
        self.assertEqual(
            len(await self.h.repo.list_documents(1, base['id'], limit=MAX_PAGE_SIZE, offset=MAX_PAGE_SIZE)),
            5,
        )
        # Offset is clamped to the signed int8 bound before it reaches SQL.
        beyond = await self.h.repo.list_documents(1, base['id'], limit=MAX_PAGE_SIZE, offset=2**63)
        self.assertEqual(beyond, [])

    async def test_search_parameters_validated_before_sql(self) -> None:
        # No search_vector in SQLite, so only the paths that never reach the
        # PostgreSQL-specific query are executed here.
        with self.assertRaises(ValueError):
            await self.h.repo.search_chunks(1, '   ')
        with self.assertRaises(ValueError):
            await self.h.repo.search_chunks(1, 'x' * (MAX_QUERY_LENGTH + 1))
        self.assertEqual(await self.h.repo.search_chunks(1, 'hello', document_id='not-a-uuid'), [])
        self.assertEqual(await self.h.repo.search_chunks(1, 'hello', knowledge_base_id='not-a-uuid'), [])


class ChunkIsolationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.h = _Harness()

    def tearDown(self) -> None:
        self.h.raw.close()

    async def _seed(self) -> None:
        """User 1 with two bases and user 2 with one, each holding one live document."""
        self.bases: dict[str, tuple[int, dict, dict]] = {}
        for owner, name in ((1, 'A1'), (1, 'A2'), (2, 'B1')):
            base = await self.h.repo.create_knowledge_base(owner, name)
            doc = await self.h.repo.create_document(owner, base['id'], filename=f'{name}.pdf')
            self.h.seed_chunk(doc['id'], 1, 0, f'{name} chunk')
            self.bases[name] = (owner, base, doc)

    async def test_chunk_queries_stay_inside_the_caller_owner(self) -> None:
        await self._seed()
        _, _, doc_own = self.bases['A1']
        _, _, doc_other = self.bases['B1']

        start = len(self.h.executed)
        self.assertEqual(len(await self.h.repo.list_chunks(1, doc_own['id'])), 1)
        listed = self.h.executed[start]
        # The executed statement carries the owner filter and both liveness
        # filters; the only SQLite rewrite was the PostgreSQL lock line.
        self.assertIn('kb.user_id = %s', listed)
        self.assertIn('kb.deleted_at IS NULL', listed)
        self.assertIn('d.deleted_at IS NULL', listed)
        self.assertNotIn('FOR SHARE', listed)

        self.assertEqual(await self.h.repo.list_chunks(2, doc_own['id']), [])
        self.assertEqual(await self.h.repo.list_chunks(1, doc_other['id']), [])
        self.assertEqual(len(await self.h.repo.list_chunks(2, doc_other['id'])), 1)

    async def test_deleting_one_base_keeps_other_bases_and_users_intact(self) -> None:
        await self._seed()
        _, doomed_base, doomed_doc = self.bases['A2']
        _, _, own_doc = self.bases['A1']
        _, _, other_doc = self.bases['B1']

        # Everything the delete issues, in order: the parent tombstone must come
        # first (it is the row lock) and the sweep must not widen beyond the owner.
        start = len(self.h.executed)
        self.assertTrue(await self.h.repo.delete_knowledge_base(1, doomed_base['id']))
        issued = self.h.executed[start:]
        self.assertEqual(len(issued), 3)
        self.assertIn('UPDATE knowledge_base SET deleted_at', issued[0])
        self.assertIn('user_id = %s AND deleted_at IS NULL', issued[0])
        # The sweep drops the liveness filter on the base it just tombstoned but
        # keeps the owner predicate, so it cannot cross users.
        self.assertIn('UPDATE document SET deleted_at', issued[1])
        self.assertNotIn('kb.deleted_at', issued[1])
        self.assertIn('user_id = %s', issued[1])
        self.assertIn('DELETE FROM document_chunk', issued[2])
        self.assertIn('kb.user_id = %s', issued[2])
        # A repeat delete is a miss, not another sweep.
        self.assertFalse(await self.h.repo.delete_knowledge_base(1, doomed_base['id']))

        # The doomed document and its chunk are gone; the other two are untouched.
        self.assertEqual(await self.h.repo.list_chunks(1, doomed_doc['id']), [])
        self.assertEqual(self.h.document_row(doomed_doc['id'])['state'], 'deleted')
        self.assertEqual(len(await self.h.repo.list_chunks(1, own_doc['id'])), 1)
        self.assertEqual(len(await self.h.repo.list_chunks(2, other_doc['id'])), 1)
        self.assertIsNone(self.h.document_row(own_doc['id'])['deleted_at'])
        self.assertIsNone(self.h.document_row(other_doc['id'])['deleted_at'])
        self.assertEqual(self.h.count('SELECT COUNT(*) FROM document_chunk'), 2)


class UploadStateTest(unittest.IsolatedAsyncioTestCase):
    """The four upload-state CAS methods, run as real SQL against SQLite.

    Coverage limits: row locks, MVCC visibility and the partial unique index
    under concurrency are PostgreSQL behaviour and are not exercised here.
    """

    def setUp(self) -> None:
        self.h = _Harness()

    def tearDown(self) -> None:
        self.h.raw.close()

    async def _uploading(self, *, owner: int = 1, sha: str | None = None, base: dict | None = None) -> dict:
        """One document taken to ``uploading`` through the repository itself."""
        if base is None:
            base = await self.h.repo.create_knowledge_base(owner, 'Papers')
        doc = await self.h.repo.create_document(owner, base['id'], filename='a.pdf', sha256=sha)
        token = str(uuid4())
        key = f'knowledge/{base["id"]}/{doc["id"]}/{token}.pdf'
        self.assertTrue(await self.h.repo.begin_upload(owner, doc['id'], token, s3_key=key))
        return {'base': base, 'doc': doc, 'token': token, 'key': key}

    async def test_begin_upload_persists_token_and_key_before_s3(self) -> None:
        start = len(self.h.executed)
        attempt = await self._uploading()
        row = self.h.document_row(attempt['doc']['id'])
        self.assertEqual(row['state'], 'uploading')
        self.assertEqual(row['lease_token'], attempt['token'])
        self.assertEqual(row['locked_by'], UPLOAD_LOCK_OWNER)
        self.assertEqual(row['s3_key'], attempt['key'])
        self.assertIsNotNone(row['locked_at'])
        # The CAS writes the token and the key in one statement, which is what
        # makes the journal key durable before the object store is contacted.
        cas = next(sql for sql in self.h.executed[start:] if "SET state = 'uploading'" in sql)
        self.assertIn("state = 'pending_upload'", cas)
        self.assertIn('lease_token IS NULL', cas)
        self.assertIn('kb.user_id = %s', cas)
        self.assertIn('kb.deleted_at IS NULL', cas)

    async def test_begin_upload_rejects_second_attempt_and_bad_input(self) -> None:
        attempt = await self._uploading()
        # The row is no longer pending, so a second attempt cannot overwrite the key.
        self.assertFalse(
            await self.h.repo.begin_upload(1, attempt['doc']['id'], str(uuid4()), s3_key='knowledge/other.pdf')
        )
        self.assertEqual(self.h.document_row(attempt['doc']['id'])['s3_key'], attempt['key'])
        self.assertFalse(
            await self.h.repo.begin_upload(2, attempt['doc']['id'], str(uuid4()), s3_key='knowledge/x.pdf')
        )
        self.assertFalse(await self.h.repo.begin_upload(1, 'not-a-uuid', str(uuid4()), s3_key='knowledge/x.pdf'))
        self.assertFalse(
            await self.h.repo.begin_upload(1, attempt['doc']['id'], 'not-a-uuid', s3_key='knowledge/x.pdf')
        )
        with self.assertRaises(ValueError):
            await self.h.repo.begin_upload(1, attempt['doc']['id'], str(uuid4()), s3_key='  ')
        with self.assertRaises(ValueError):
            await self.h.repo.begin_upload(1, attempt['doc']['id'], str(uuid4()), s3_key=None)  # type: ignore[arg-type]
        self.assertEqual(self.h.document_row(attempt['doc']['id'])['s3_key'], attempt['key'])

    async def test_begin_upload_needs_own_live_base_and_untouched_counters(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Papers')
        doc = await self.h.repo.create_document(1, base['id'], filename='a.pdf', s3_key='free-existing.pdf')
        # An existing key means an earlier journal entry this path must not clobber.
        self.assertFalse(await self.h.repo.begin_upload(1, doc['id'], str(uuid4()), s3_key='knowledge/x.pdf'))
        self.assertEqual(self.h.document_row(doc['id'])['s3_key'], 'free-existing.pdf')
        self.assertFalse(await self.h.repo.begin_upload(2, doc['id'], str(uuid4()), s3_key='knowledge/x.pdf'))
        self.assertTrue(await self.h.repo.delete_knowledge_base(1, base['id']))
        self.assertFalse(await self.h.repo.begin_upload(1, doc['id'], str(uuid4()), s3_key='knowledge/x.pdf'))
        self.assertEqual(self.h.document_row(doc['id'])['state'], 'deleted')

        other = await self.h.repo.create_knowledge_base(1, 'Other')
        fresh = await self.h.repo.create_document(1, other['id'], filename='b.pdf')
        self.assertTrue(await self.h.repo.begin_upload(1, fresh['id'], str(uuid4()), s3_key='knowledge/b.pdf'))
        row = self.h.document_row(fresh['id'])
        self.assertEqual(row['attempts'], 0)
        self.assertEqual(row['version'], 1)

    async def test_finish_upload_queues_and_clears_lease(self) -> None:
        attempt = await self._uploading()
        digest = hashlib.sha256(b'payload').hexdigest()
        self.assertEqual(
            await self.h.repo.finish_upload(1, attempt['doc']['id'], attempt['token'], size_bytes=7, sha256=digest),
            'queued',
        )
        row = self.h.document_row(attempt['doc']['id'])
        self.assertEqual(row['state'], 'queued')
        self.assertEqual(row['size_bytes'], 7)
        self.assertEqual(row['sha256'], digest)
        self.assertEqual(row['s3_key'], attempt['key'])
        self.assertIsNone(row['lease_token'])
        self.assertIsNone(row['locked_by'])
        self.assertIsNone(row['locked_at'])
        self.assertIsNone(row['error_code'])
        self.assertEqual(row['attempts'], 0)
        self.assertEqual(row['version'], 1)
        # The finishing UPDATE itself carries owner, liveness, state and token.
        issued = self.h.executed[-2]
        self.assertIn("state = 'uploading'", issued)
        self.assertIn('lease_token = %s', issued)
        self.assertIn('kb.user_id = %s', issued)

    async def test_finish_upload_is_stale_for_wrong_token_and_foreign_or_deleted_rows(self) -> None:
        attempt = await self._uploading()
        digest = hashlib.sha256(b'payload').hexdigest()
        signature = dict(size_bytes=7, sha256=digest)
        baseline = self.h.document_row(attempt['doc']['id'])['updated_at']

        self.assertEqual(await self.h.repo.finish_upload(1, attempt['doc']['id'], str(uuid4()), **signature), 'stale')
        self.assertEqual(
            await self.h.repo.finish_upload(2, attempt['doc']['id'], attempt['token'], **signature), 'stale'
        )
        self.assertEqual(await self.h.repo.finish_upload(1, str(uuid4()), attempt['token'], **signature), 'stale')
        self.assertEqual(await self.h.repo.finish_upload(1, 'not-a-uuid', attempt['token'], **signature), 'stale')
        row = self.h.document_row(attempt['doc']['id'])
        self.assertEqual(row['state'], 'uploading')
        self.assertIsNone(row['size_bytes'])
        self.assertIsNone(row['sha256'])
        self.assertEqual(row['updated_at'], baseline)

        # An unrelated document in the same base is untouched by those misses.
        sibling = await self.h.repo.create_document(1, attempt['base']['id'], filename='sibling.pdf')
        self.assertEqual(await self.h.repo.finish_upload(1, sibling['id'], attempt['token'], **signature), 'stale')
        self.assertIsNone(self.h.document_row(sibling['id'])['sha256'])

    async def test_finish_upload_is_stale_after_parent_or_document_delete(self) -> None:
        attempt = await self._uploading()
        digest = hashlib.sha256(b'payload').hexdigest()
        self.assertTrue(await self.h.repo.delete_document(1, attempt['doc']['id']))
        self.assertEqual(
            await self.h.repo.finish_upload(1, attempt['doc']['id'], attempt['token'], size_bytes=7, sha256=digest),
            'stale',
        )
        self.assertEqual(self.h.document_row(attempt['doc']['id'])['state'], 'deleted')

        parented = await self._uploading()
        self.assertTrue(await self.h.repo.delete_knowledge_base(1, parented['base']['id']))
        self.assertEqual(
            await self.h.repo.finish_upload(1, parented['doc']['id'], parented['token'], size_bytes=7, sha256=digest),
            'stale',
        )
        self.assertEqual(self.h.document_row(parented['doc']['id'])['state'], 'deleted')

    async def test_finish_upload_rejects_a_client_claimed_hash_or_size(self) -> None:
        attempt = await self._uploading()
        digest = hashlib.sha256(b'payload').hexdigest()
        for bad_size in (0, -1, True, 1.0, '7'):
            with self.assertRaises(ValueError, msg=f'accepted size {bad_size!r}'):
                await self.h.repo.finish_upload(
                    1, attempt['doc']['id'], attempt['token'], size_bytes=bad_size, sha256=digest
                )
        for bad_sha in ('', digest.upper(), digest[:63], digest + 'a', 'z' * 64):
            with self.assertRaises(ValueError, msg=f'accepted sha {bad_sha!r}'):
                await self.h.repo.finish_upload(1, attempt['doc']['id'], attempt['token'], size_bytes=7, sha256=bad_sha)
        # A late second finish cannot overwrite the queued row: the state moved on.
        self.assertEqual(
            await self.h.repo.finish_upload(1, attempt['doc']['id'], attempt['token'], size_bytes=7, sha256=digest),
            'queued',
        )
        self.assertEqual(
            await self.h.repo.finish_upload(1, attempt['doc']['id'], attempt['token'], size_bytes=1, sha256=digest),
            'stale',
        )
        self.assertEqual(self.h.document_row(attempt['doc']['id'])['size_bytes'], 7)

    async def test_finish_upload_duplicate_rolls_back_only_the_statement(self) -> None:
        digest = hashlib.sha256(b'same bytes').hexdigest()
        winner = await self._uploading()
        self.assertEqual(
            await self.h.repo.finish_upload(1, winner['doc']['id'], winner['token'], size_bytes=10, sha256=digest),
            'queued',
        )
        loser = await self._uploading(base=winner['base'])
        self.assertEqual(
            await self.h.repo.finish_upload(1, loser['doc']['id'], loser['token'], size_bytes=10, sha256=digest),
            'duplicate',
        )
        row = self.h.document_row(loser['doc']['id'])
        self.assertEqual(row['state'], 'uploading')
        self.assertIsNone(row['sha256'])
        self.assertEqual(row['s3_key'], loser['key'])
        # The savepoint was rolled back and released, so later SQL still runs and
        # the caller's outer transaction can still be committed.
        savepoint = f'SAVEPOINT {FINISH_SAVEPOINT}'
        self.assertIn(savepoint, self.h.executed)
        self.assertIn(f'ROLLBACK TO SAVEPOINT {FINISH_SAVEPOINT}', self.h.executed)
        self.assertEqual(self.h.executed.count(f'RELEASE SAVEPOINT {FINISH_SAVEPOINT}'), 2)
        self.assertIsNotNone(await self.h.repo.get_document(1, winner['doc']['id']))
        self.h.raw.commit()
        self.assertEqual(self.h.document_row(winner['doc']['id'])['state'], 'queued')

    async def test_finish_upload_does_not_swallow_another_constraint(self) -> None:
        # A retitled diagnostic proves the duplicate branch is keyed on the index
        # name: the same SQLite conflict is then re-raised instead of answered.
        self.h.raw.close()
        self.h = _Harness(sha_constraint='document_active_sha256_old')
        digest = hashlib.sha256(b'same bytes').hexdigest()
        winner = await self._uploading()
        await self.h.repo.finish_upload(1, winner['doc']['id'], winner['token'], size_bytes=10, sha256=digest)
        loser = await self._uploading(base=winner['base'])
        with self.assertRaises(psycopg.errors.UniqueViolation) as caught:
            await self.h.repo.finish_upload(1, loser['doc']['id'], loser['token'], size_bytes=10, sha256=digest)
        # The re-raised error is this double's, with the retitled diagnostic the
        # repository refused to treat as a duplicate.
        self.assertEqual(caught.exception.diag.constraint_name, 'document_active_sha256_old')
        self.assertEqual(self.h.document_row(loser['doc']['id'])['state'], 'uploading')
        # The savepoint was still closed, so the connection is usable.
        self.assertFalse(await self.h.repo.begin_upload(1, loser['doc']['id'], str(uuid4()), s3_key='knowledge/x.pdf'))

    async def test_finish_upload_raises_when_savepoint_rollback_fails(self) -> None:
        # A duplicate conflict whose ROLLBACK TO cannot run: the transaction was
        # not restored, so the method must not answer ``duplicate``. A successful
        # call only issues RELEASE, which this fault does not match.
        digest = hashlib.sha256(b'same bytes').hexdigest()
        winner = await self._uploading()
        await self.h.repo.finish_upload(1, winner['doc']['id'], winner['token'], size_bytes=10, sha256=digest)
        loser = await self._uploading(base=winner['base'])
        self.h.fail_next_savepoint_cleanup('rollback')
        with self.assertRaises(RuntimeError) as caught:
            await self.h.repo.finish_upload(1, loser['doc']['id'], loser['token'], size_bytes=10, sha256=digest)
        self.assertIn('savepoint cleanup failure: rollback', str(caught.exception))

    async def test_finish_upload_raises_when_savepoint_release_fails(self) -> None:
        # ROLLBACK TO succeeded but RELEASE did not; the savepoint cleanup is still
        # incomplete, so the method must propagate instead of reporting a dedupe.
        # The fault is armed after the winner queued, so it can only hit the
        # duplicate call's cleanup, never the successful call's normal release.
        digest = hashlib.sha256(b'same bytes').hexdigest()
        winner = await self._uploading()
        await self.h.repo.finish_upload(1, winner['doc']['id'], winner['token'], size_bytes=10, sha256=digest)
        loser = await self._uploading(base=winner['base'])
        self.h.fail_next_savepoint_cleanup('release')
        with self.assertRaises(RuntimeError) as caught:
            await self.h.repo.finish_upload(1, loser['doc']['id'], loser['token'], size_bytes=10, sha256=digest)
        self.assertIn('savepoint cleanup failure: release', str(caught.exception))

    async def test_fail_upload_rejects_a_non_bool_cleanup_flag(self) -> None:
        attempt = await self._uploading()
        signature = dict(error_code='document.upload_failed', error_message='The document could not be stored.')
        baseline = self.h.document_row(attempt['doc']['id'])
        for bad in ('false', 'true', 'True', 1, 0, None):
            with self.assertRaises(ValueError, msg=f'accepted cleanup_succeeded={bad!r}'):
                await self.h.repo.fail_upload(
                    1, attempt['doc']['id'], attempt['token'], cleanup_succeeded=bad, **signature
                )
        # The rejected calls reached no SQL: state, key and token are untouched.
        row = self.h.document_row(attempt['doc']['id'])
        self.assertEqual(row['state'], baseline['state'])
        self.assertEqual(row['s3_key'], attempt['key'])
        self.assertEqual(row['lease_token'], attempt['token'])
        self.assertEqual(row['error_code'], baseline['error_code'])
        self.assertEqual(row['updated_at'], baseline['updated_at'])
        self.assertEqual(baseline['state'], 'uploading')

    async def test_finish_upload_is_visible_to_the_outer_rollback(self) -> None:
        attempt = await self._uploading()
        # Model the caller: commit the short begin-upload transaction, run the long
        # upload, then open the transaction that will hold the finishing statement.
        self.h.raw.commit()
        self.h.raw.execute('BEGIN')
        digest = hashlib.sha256(b'payload').hexdigest()
        self.assertEqual(
            await self.h.repo.finish_upload(1, attempt['doc']['id'], attempt['token'], size_bytes=7, sha256=digest),
            'queued',
        )
        await self.h.outer_transaction()
        row = self.h.document_row(attempt['doc']['id'])
        # finish_upload worked in the caller's transaction only; a rollback undoes it.
        self.assertEqual(row['state'], 'uploading')
        self.assertIsNone(row['sha256'])
        self.assertEqual(row['lease_token'], attempt['token'])

    async def test_fail_upload_journals_the_key_until_cleanup_is_confirmed(self) -> None:
        attempt = await self._uploading()
        code = 'document.upload_failed'
        message = 'The document could not be stored.'
        self.assertTrue(
            await self.h.repo.fail_upload(
                1, attempt['doc']['id'], attempt['token'], error_code=code, error_message=message
            )
        )
        row = self.h.document_row(attempt['doc']['id'])
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['error_code'], code)
        self.assertEqual(row['error_message'], message)
        # No storage confirmation, so the object is still on the journal.
        self.assertEqual(row['s3_key'], attempt['key'])
        self.assertIsNone(row['lease_token'])
        self.assertEqual(row['attempts'], 0)
        self.assertEqual(row['version'], 1)

        other = await self._uploading()
        self.assertTrue(
            await self.h.repo.fail_upload(
                1, other['doc']['id'], other['token'], error_code=code, error_message=message, cleanup_succeeded=True
            )
        )
        self.assertIsNone(self.h.document_row(other['doc']['id'])['s3_key'])

    async def test_fail_upload_misses_leave_zero_side_effects(self) -> None:
        attempt = await self._uploading()
        signature = dict(error_code='document.upload_failed', error_message='The document could not be stored.')
        baseline = self.h.document_row(attempt['doc']['id'])['updated_at']
        self.assertFalse(await self.h.repo.fail_upload(1, attempt['doc']['id'], str(uuid4()), **signature))
        self.assertFalse(await self.h.repo.fail_upload(2, attempt['doc']['id'], attempt['token'], **signature))
        self.assertFalse(await self.h.repo.fail_upload(1, str(uuid4()), attempt['token'], **signature))
        self.assertFalse(await self.h.repo.fail_upload(1, 'not-a-uuid', attempt['token'], **signature))
        row = self.h.document_row(attempt['doc']['id'])
        self.assertEqual(row['state'], 'uploading')
        self.assertIsNone(row['error_code'])
        self.assertEqual(row['s3_key'], attempt['key'])
        self.assertEqual(row['updated_at'], baseline)

        # A deleted document never comes back, and failure text is bounded ASCII.
        self.assertTrue(await self.h.repo.delete_document(1, attempt['doc']['id']))
        self.assertFalse(await self.h.repo.fail_upload(1, attempt['doc']['id'], attempt['token'], **signature))
        self.assertEqual(self.h.document_row(attempt['doc']['id'])['state'], 'deleted')
        with self.assertRaises(ValueError):
            await self.h.repo.fail_upload(1, attempt['doc']['id'], attempt['token'], error_code='', error_message='x')
        with self.assertRaises(ValueError):
            await self.h.repo.fail_upload(
                1, attempt['doc']['id'], attempt['token'], error_code='中文', error_message='x'
            )
        with self.assertRaises(ValueError):
            await self.h.repo.fail_upload(
                1,
                attempt['doc']['id'],
                attempt['token'],
                error_code='c' * (MAX_ERROR_CODE_LENGTH + 1),
                error_message='x',
            )
        with self.assertRaises(ValueError):
            await self.h.repo.fail_upload(
                1,
                attempt['doc']['id'],
                attempt['token'],
                error_code='code',
                error_message='m' * (MAX_ERROR_MESSAGE_LENGTH + 1),
            )

    async def test_retry_upload_returns_to_pending_and_invalidates_the_old_token(self) -> None:
        attempt = await self._uploading()
        digest = hashlib.sha256(b'payload').hexdigest()
        await self.h.repo.fail_upload(
            1, attempt['doc']['id'], attempt['token'], error_code='document.upload_failed', error_message='failed'
        )
        # The journalled key is not confirmed cleaned, so the caller repeats with it.
        self.assertTrue(await self.h.repo.retry_upload(1, attempt['doc']['id'], cleaned_key=attempt['key']))
        row = self.h.document_row(attempt['doc']['id'])
        self.assertEqual(row['state'], 'pending_upload')
        self.assertIsNone(row['s3_key'])
        self.assertIsNone(row['error_code'])
        self.assertIsNone(row['lease_token'])
        self.assertEqual(row['attempts'], 0)
        self.assertEqual(row['version'], 1)
        # The superseded attempt token is dead: a late finish neither queues nor
        # fails the row.
        self.assertEqual(
            await self.h.repo.finish_upload(1, attempt['doc']['id'], attempt['token'], size_bytes=7, sha256=digest),
            'stale',
        )
        self.assertFalse(
            await self.h.repo.fail_upload(
                1, attempt['doc']['id'], attempt['token'], error_code='document.upload_failed', error_message='late'
            )
        )
        self.assertEqual(self.h.document_row(attempt['doc']['id'])['state'], 'pending_upload')

    async def test_retry_upload_needs_exact_confirmed_cleaned_key(self) -> None:
        attempt = await self._uploading()
        await self.h.repo.fail_upload(
            1, attempt['doc']['id'], attempt['token'], error_code='document.upload_failed', error_message='failed'
        )
        signature = {'error_code': 'document.upload_failed', 'error_message': 'failed'}
        self.assertFalse(await self.h.repo.retry_upload(1, attempt['doc']['id']))
        self.assertFalse(await self.h.repo.retry_upload(1, attempt['doc']['id'], cleaned_key='knowledge/x.pdf'))
        self.assertFalse(await self.h.repo.retry_upload(1, attempt['doc']['id'], cleaned_key=attempt['key'] + '0'))
        row = self.h.document_row(attempt['doc']['id'])
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['s3_key'], attempt['key'])
        self.assertEqual(row['error_code'], signature['error_code'])
        # The same refusal for a foreign owner and an unknown id.
        self.assertFalse(await self.h.repo.retry_upload(2, attempt['doc']['id'], cleaned_key=attempt['key']))
        self.assertFalse(await self.h.repo.retry_upload(1, str(uuid4()), cleaned_key=attempt['key']))
        with self.assertRaises(ValueError):
            await self.h.repo.retry_upload(1, attempt['doc']['id'], cleaned_key=123)  # type: ignore[arg-type]
        self.assertTrue(await self.h.repo.retry_upload(1, attempt['doc']['id'], cleaned_key=attempt['key']))

    async def test_retry_upload_never_revives_other_states_or_no_key_failures(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Papers')
        for state in ('queued', 'ready', 'processing', 'unsupported'):
            doc = await self.h.repo.create_document(1, base['id'], filename=f'{state}.pdf')
            self.h.raw.execute(
                'UPDATE document SET state = ?, s3_key = ? WHERE id = ?',
                (state, 'knowledge/x.pdf', doc['id']),
            )
            self.h.raw.commit()
            self.assertFalse(await self.h.repo.retry_upload(1, doc['id'], cleaned_key='knowledge/x.pdf'))
            self.assertEqual(self.h.document_row(doc['id'])['state'], state)

        # A deleted upload failure stays deleted.
        deleted = await self._uploading()
        await self.h.repo.fail_upload(
            1, deleted['doc']['id'], deleted['token'], error_code='document.upload_failed', error_message='failed'
        )
        self.assertTrue(await self.h.repo.delete_document(1, deleted['doc']['id']))
        self.assertFalse(await self.h.repo.retry_upload(1, deleted['doc']['id'], cleaned_key=deleted['key']))
        self.assertEqual(self.h.document_row(deleted['doc']['id'])['state'], 'deleted')

        # A failure with a content hash is a parse failure, not an upload retry.
        parsed = await self.h.repo.create_document(1, base['id'], filename='parsed.pdf')
        digest = hashlib.sha256(b'parsed').hexdigest()
        self.h.raw.execute(
            "UPDATE document SET state = 'failed', sha256 = ?, error_code = 'document.parse_failed' WHERE id = ?",
            (digest, parsed['id']),
        )
        self.h.raw.commit()
        self.assertFalse(await self.h.repo.retry_upload(1, parsed['id']))
        self.assertEqual(self.h.document_row(parsed['id'])['state'], 'failed')

        # No journalled key: nothing to clean, so no confirmation is needed.
        keyless = await self.h.repo.create_document(1, base['id'], filename='keyless.pdf')
        self.h.raw.execute("UPDATE document SET state = 'failed' WHERE id = ?", (keyless['id'],))
        self.h.raw.commit()
        self.assertTrue(await self.h.repo.retry_upload(1, keyless['id']))
        self.assertEqual(self.h.document_row(keyless['id'])['state'], 'pending_upload')

    async def test_sha_uniqueness_is_per_knowledge_base(self) -> None:
        digest = hashlib.sha256(b'same bytes').hexdigest()
        attempt = await self._uploading()
        self.assertEqual(
            await self.h.repo.finish_upload(1, attempt['doc']['id'], attempt['token'], size_bytes=10, sha256=digest),
            'queued',
        )
        # Another base of the same user is allowed the same bytes.
        other = await self._uploading()
        self.assertEqual(
            await self.h.repo.finish_upload(1, other['doc']['id'], other['token'], size_bytes=10, sha256=digest),
            'queued',
        )
        # A second live copy in the winning base is the dedupe case.
        losing = await self.h.repo.create_document(1, attempt['base']['id'], filename='dup.pdf')
        token = str(uuid4())
        self.assertTrue(await self.h.repo.begin_upload(1, losing['id'], token, s3_key='knowledge/dup.pdf'))
        self.assertEqual(
            await self.h.repo.finish_upload(1, losing['id'], token, size_bytes=10, sha256=digest), 'duplicate'
        )


class ImportStateTest(unittest.IsolatedAsyncioTestCase):
    """The five import-state CAS methods, run as real SQL against SQLite.

    Coverage limits: row locks, MVCC visibility and concurrent claims are
    PostgreSQL behaviour and are not exercised here. SQLite proves the ordinary
    SQL, the pre-SQL validation and the transaction boundaries only.
    """

    def setUp(self) -> None:
        self.h = _Harness()

    def tearDown(self) -> None:
        self.h.raw.close()

    async def _queued(
        self,
        *,
        owner: int = 1,
        base: dict | None = None,
        mime: str | None = 'application/pdf',
        size: int = 10,
        sha: str | None = None,
    ) -> dict:
        """One document taken to a claimable ``queued`` row via the repository."""
        if base is None:
            base = await self.h.repo.create_knowledge_base(owner, 'Papers')
        doc = await self.h.repo.create_document(owner, base['id'], filename='a.pdf', mime_type=mime)
        token = str(uuid4())
        key = f'knowledge/{doc["id"]}/a.pdf'
        self.assertTrue(await self.h.repo.begin_upload(owner, doc['id'], token, s3_key=key))
        digest = sha or hashlib.sha256(doc['id'].encode()).hexdigest()
        self.assertEqual(
            await self.h.repo.finish_upload(owner, doc['id'], token, size_bytes=size, sha256=digest),
            'queued',
        )
        return {'base': base, 'doc': doc, 'token': token, 'key': key, 'sha': digest}

    async def test_list_queued_imports_filters_orders_and_bounds(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Papers')
        valid = [await self._queued(base=base), await self._queued(base=base)]
        foreign = await self._queued(owner=2)

        # Noise rows that must never reach a worker: wrong state, not uploaded,
        # an unsupported MIME, zero size, a missing key/hash and a live lease.
        uploading = await self.h.repo.create_document(1, base['id'], filename='up.pdf', mime_type='application/pdf')
        await self.h.repo.begin_upload(1, uploading['id'], str(uuid4()), s3_key='knowledge/up.pdf')
        unsupported = await self.h.repo.create_document(1, base['id'], filename='csv.pdf', mime_type='text/csv')
        csv_token = str(uuid4())
        self.assertTrue(await self.h.repo.begin_upload(1, unsupported['id'], csv_token, s3_key='knowledge/csv.pdf'))
        self.assertEqual(
            await self.h.repo.finish_upload(
                1, unsupported['id'], csv_token, size_bytes=10, sha256=hashlib.sha256(b'csv').hexdigest()
            ),
            'queued',
        )
        zero_size = await self._queued(base=base)
        self.h.raw.execute('UPDATE document SET size_bytes = 0 WHERE id = ?', (zero_size['doc']['id'],))
        keyless = await self._queued(base=base)
        self.h.raw.execute('UPDATE document SET s3_key = NULL WHERE id = ?', (keyless['doc']['id'],))
        hashedless = await self._queued(base=base)
        self.h.raw.execute('UPDATE document SET sha256 = NULL WHERE id = ?', (hashedless['doc']['id'],))
        leased = await self._queued(base=base)
        self.h.raw.execute(
            "UPDATE document SET locked_by = 'worker-1', lease_token = ? WHERE id = ?",
            (str(uuid4()), leased['doc']['id']),
        )
        promoted = await self._queued(base=base)
        self.h.raw.execute("UPDATE document SET state = 'ready' WHERE id = ?", (promoted['doc']['id'],))
        gone = await self._queued(base=base)
        self.assertTrue(await self.h.repo.delete_document(1, gone['doc']['id']))

        # Ordering is created_at/id, not the insertion clock.
        self.h.raw.execute(
            "UPDATE document SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (valid[1]['doc']['id'],)
        )
        self.h.raw.execute(
            "UPDATE document SET created_at = '2019-01-01T00:00:00+00:00' WHERE id = ?", (valid[0]['doc']['id'],)
        )

        listed = await self.h.repo.list_queued_imports()
        self.assertEqual(
            {row['id'] for row in listed},
            {valid[0]['doc']['id'], valid[1]['doc']['id'], foreign['doc']['id']},
        )
        own_order = [row['id'] for row in listed if row['id'] != foreign['doc']['id']]
        self.assertEqual(own_order, [valid[0]['doc']['id'], valid[1]['doc']['id']])
        # The candidate carries the base owner and the upload journal; it is an
        # internal row and is never an HTTP payload.
        self.assertEqual(next(row for row in listed if row['id'] == foreign['doc']['id'])['user_id'], 2)
        own_row = next(row for row in listed if row['id'] == valid[0]['doc']['id'])
        self.assertEqual(own_row['s3_key'], valid[0]['key'])
        # Listing is not a claim: the row is untouched.
        self.assertEqual(self.h.document_row(valid[0]['doc']['id'])['state'], 'queued')

        self.assertEqual(len(await self.h.repo.list_queued_imports(limit=1)), 1)
        self.assertEqual(len(await self.h.repo.list_queued_imports(limit=10**9)), len(listed))

    async def test_claim_import_is_a_cas_with_attempts(self) -> None:
        attempt = await self._queued()
        token = str(uuid4())
        claimed = await self.h.repo.claim_import(1, attempt['doc']['id'], 'worker-7', token)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed['state'], 'processing')
        self.assertEqual(claimed['id'], attempt['doc']['id'])
        self.assertEqual(claimed['locked_by'], 'worker-7')
        self.assertEqual(claimed['lease_token'], token)
        self.assertEqual(claimed['attempts'], 1)
        self.assertEqual(claimed['version'], 1)
        self.assertEqual(claimed['s3_key'], attempt['key'])
        self.assertIsNotNone(claimed['locked_at'])

        # The row is no longer free, so nobody else (other worker, other owner,
        # other token) can claim it and its lease does not move.
        self.assertIsNone(await self.h.repo.claim_import(1, attempt['doc']['id'], 'worker-8', str(uuid4())))
        self.assertIsNone(await self.h.repo.claim_import(2, attempt['doc']['id'], 'worker-7', str(uuid4())))
        self.assertIsNone(await self.h.repo.claim_import(1, attempt['doc']['id'], 'worker-7', str(uuid4())))
        self.assertIsNone(await self.h.repo.claim_import(1, str(uuid4()), 'worker-7', str(uuid4())))
        self.assertIsNone(await self.h.repo.claim_import(1, 'not-a-uuid', 'worker-7', str(uuid4())))
        self.assertIsNone(await self.h.repo.claim_import(1, attempt['doc']['id'], 'worker-7', 'not-a-uuid'))
        row = self.h.document_row(attempt['doc']['id'])
        self.assertEqual(row['lease_token'], token)
        self.assertEqual(row['attempts'], 1)

    async def test_claim_import_rejects_bad_workers_and_missing_source(self) -> None:
        attempt = await self._queued()
        for bad in ('', '   ', 'upload', '上传', 7, None, 'w' * (MAX_WORKER_ID_LENGTH + 1)):
            with self.assertRaises(ValueError, msg=f'accepted worker_id={bad!r}'):
                await self.h.repo.claim_import(1, attempt['doc']['id'], bad, str(uuid4()))  # type: ignore[arg-type]
        row = self.h.document_row(attempt['doc']['id'])
        self.assertEqual(row['state'], 'queued')
        self.assertEqual(row['attempts'], 0)
        self.assertIsNone(row['lease_token'])

        # A queued row that lost its uploaded metadata is not claimable.
        for column in ('sha256', 's3_key', 'size_bytes', 'mime_type'):
            sourceless = await self._queued()
            self.h.raw.execute(f'UPDATE document SET {column} = NULL WHERE id = ?', (sourceless['doc']['id'],))
            claimed = await self.h.repo.claim_import(1, sourceless['doc']['id'], 'worker-1', str(uuid4()))
            self.assertIsNone(claimed)
            self.assertEqual(self.h.document_row(sourceless['doc']['id'])['state'], 'queued')

        parented = await self._queued()
        self.assertTrue(await self.h.repo.delete_knowledge_base(1, parented['base']['id']))
        self.assertIsNone(await self.h.repo.claim_import(1, parented['doc']['id'], 'worker-1', str(uuid4())))
        self.assertEqual(self.h.document_row(parented['doc']['id'])['state'], 'deleted')

    async def test_complete_import_publishes_all_chunks_and_clears_lease(self) -> None:
        attempt = await self._queued()
        token = str(uuid4())
        await self.h.repo.claim_import(1, attempt['doc']['id'], 'worker-1', token)
        self.h.seed_chunk(attempt['doc']['id'], 1, 0, 'stale derived text')
        chunks = [
            Chunk(1, 0, 'alpha'),
            Chunk(1, 1, 'alpha'),  # duplicate text is legal
            Chunk(3, 0, 'omega'),  # page 2 has no chunk at all
        ]
        self.assertTrue(await self.h.repo.complete_import(1, attempt['doc']['id'], token, page_count=3, chunks=chunks))
        row = self.h.document_row(attempt['doc']['id'])
        self.assertEqual(row['state'], 'ready')
        self.assertEqual(row['page_count'], 3)
        self.assertEqual(row['version'], 2)
        self.assertIsNone(row['lease_token'])
        self.assertIsNone(row['locked_by'])
        self.assertIsNone(row['locked_at'])
        self.assertEqual(row['s3_key'], attempt['key'])
        self.assertEqual(row['attempts'], 1)
        listed = await self.h.repo.list_chunks(1, attempt['doc']['id'])
        self.assertEqual(
            [(item['page_no'], item['ordinal'], item['text']) for item in listed],
            [(1, 0, 'alpha'), (1, 1, 'alpha'), (3, 0, 'omega')],
        )

    async def test_complete_import_inserts_in_bounded_batches(self) -> None:
        attempt = await self._queued()
        token = str(uuid4())
        await self.h.repo.claim_import(1, attempt['doc']['id'], 'worker-1', token)
        chunks = [Chunk(1, index, f'chunk-{index}') for index in range(IMPORT_CHUNK_BATCH_SIZE + 5)]
        start = len(self.h.executed)
        self.assertTrue(await self.h.repo.complete_import(1, attempt['doc']['id'], token, page_count=1, chunks=chunks))
        batches = [sql for sql in self.h.executed[start:] if 'INSERT INTO document_chunk' in sql]
        self.assertEqual(len(batches), 2)
        inserted = self.h.count('SELECT COUNT(*) FROM document_chunk WHERE document_id = ?', (attempt['doc']['id'],))
        self.assertEqual(inserted, IMPORT_CHUNK_BATCH_SIZE + 5)

    async def test_complete_import_validates_everything_before_sql(self) -> None:
        attempt = await self._queued()
        token = str(uuid4())
        await self.h.repo.claim_import(1, attempt['doc']['id'], 'worker-1', token)
        baseline = len(self.h.executed)
        good = [Chunk(1, 0, 'ok')]

        for page_count in (0, -1, True, 1.0, MAX_PDF_PAGES + 1):
            with self.assertRaises(ValueError, msg=f'accepted page_count={page_count!r}'):
                await self.h.repo.complete_import(1, attempt['doc']['id'], token, page_count=page_count, chunks=good)
        overflow_total = [
            Chunk(1, index, 'x' * MAX_CHUNK_CHARS) for index in range(MAX_TEXT_CHARS // MAX_CHUNK_CHARS + 1)
        ]
        bad_chunk_sets: list[object] = [
            [],
            'not-a-sequence',
            [('page', 0, 'x')],
            [Chunk(0, 0, 'x')],
            [Chunk(2, 0, 'x')],
            [Chunk(1, -1, 'x')],
            [Chunk(1, True, 'x')],
            [Chunk(1, 0, '')],
            [Chunk(1, 0, 'nul\x00byte')],
            [Chunk(1, 0, 'x' * (MAX_CHUNK_CHARS + 1))],
            [Chunk(2, 0, 'b'), Chunk(1, 0, 'a')],
            [Chunk(1, 1, 'b'), Chunk(1, 1, 'a')],
            overflow_total,
            [Chunk(1, index, 'x') for index in range(MAX_CHUNKS + 1)],
        ]
        for chunks in bad_chunk_sets:
            with self.assertRaises(ValueError, msg=f'accepted chunks={type(chunks).__name__}'):
                await self.h.repo.complete_import(
                    1,
                    attempt['doc']['id'],
                    token,
                    page_count=1,
                    chunks=chunks,  # type: ignore[arg-type]
                )

        # Every rejection happened before the first SQL statement.
        self.assertEqual(len(self.h.executed), baseline)
        row = self.h.document_row(attempt['doc']['id'])
        self.assertEqual(row['state'], 'processing')
        self.assertEqual(row['version'], 1)
        self.assertEqual(self.h.count('SELECT COUNT(*) FROM document_chunk'), 0)

    async def test_complete_import_stale_cas_leaves_chunks_untouched(self) -> None:
        attempt = await self._queued()
        token = str(uuid4())
        await self.h.repo.claim_import(1, attempt['doc']['id'], 'worker-1', token)
        self.h.seed_chunk(attempt['doc']['id'], 1, 0, 'old')
        chunks = [Chunk(1, 0, 'new')]
        self.assertFalse(
            await self.h.repo.complete_import(1, attempt['doc']['id'], str(uuid4()), page_count=1, chunks=chunks)
        )
        self.assertFalse(await self.h.repo.complete_import(2, attempt['doc']['id'], token, page_count=1, chunks=chunks))
        self.assertFalse(await self.h.repo.complete_import(1, str(uuid4()), token, page_count=1, chunks=chunks))
        self.assertFalse(await self.h.repo.complete_import(1, 'not-a-uuid', token, page_count=1, chunks=chunks))
        self.assertFalse(
            await self.h.repo.complete_import(1, attempt['doc']['id'], 'not-a-uuid', page_count=1, chunks=chunks)
        )
        row = self.h.document_row(attempt['doc']['id'])
        self.assertEqual(row['state'], 'processing')
        self.assertEqual(row['version'], 1)
        kept = await self.h.repo.list_chunks(1, attempt['doc']['id'])
        self.assertEqual([item['text'] for item in kept], ['old'])

        # A row in ``processing`` under the reserved uploader label is never
        # publishable, even though its token matches.
        forged = await self._queued(base=attempt['base'])
        forged_token = str(uuid4())
        self.h.raw.execute(
            "UPDATE document SET state = 'processing', locked_by = ?, lease_token = ? WHERE id = ?",
            (UPLOAD_LOCK_OWNER, forged_token, forged['doc']['id']),
        )
        self.h.raw.commit()
        self.assertFalse(
            await self.h.repo.complete_import(1, forged['doc']['id'], forged_token, page_count=1, chunks=chunks)
        )
        self.assertEqual(self.h.document_row(forged['doc']['id'])['state'], 'processing')

    async def test_complete_import_rolls_back_with_the_outer_transaction(self) -> None:
        attempt = await self._queued()
        token = str(uuid4())
        await self.h.repo.claim_import(1, attempt['doc']['id'], 'worker-1', token)
        # The worker committed the claim before parsing; the publish below runs in
        # the caller's own short transaction, which is the one that fails.
        self.h.raw.commit()
        self.h.seed_chunk(attempt['doc']['id'], 1, 0, 'old')
        chunks = [Chunk(1, index, f'chunk-{index}') for index in range(IMPORT_CHUNK_BATCH_SIZE + 1)]
        # The second batch fails after the first was inserted, so the ready UPDATE,
        # the old-chunk delete and the partial insert must all be undone by the
        # caller's rollback and the document must still be processing.
        self.h.fail_chunk_insert_after(1)
        with self.assertRaises(RuntimeError) as caught:
            await self.h.repo.complete_import(1, attempt['doc']['id'], token, page_count=1, chunks=chunks)
        self.assertIn('simulated chunk insert failure', str(caught.exception))
        await self.h.outer_transaction()
        row = self.h.document_row(attempt['doc']['id'])
        self.assertEqual(row['state'], 'processing')
        self.assertEqual(row['version'], 1)
        self.assertEqual(row['lease_token'], token)
        kept = await self.h.repo.list_chunks(1, attempt['doc']['id'])
        self.assertEqual([item['text'] for item in kept], ['old'])

    async def test_fail_import_keeps_source_and_distinguishes_upload_failure(self) -> None:
        attempt = await self._queued()
        token = str(uuid4())
        await self.h.repo.claim_import(1, attempt['doc']['id'], 'worker-1', token)
        self.assertTrue(
            await self.h.repo.fail_import(
                1,
                attempt['doc']['id'],
                token,
                error_code='document.needs_ocr',
                error_message='The PDF has no text layer.',
            )
        )
        row = self.h.document_row(attempt['doc']['id'])
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['error_code'], 'document.needs_ocr')
        self.assertEqual(row['error_message'], 'The PDF has no text layer.')
        self.assertIsNone(row['lease_token'])
        self.assertIsNone(row['locked_by'])
        # The uploaded source survives for the import retry.
        self.assertEqual(row['s3_key'], attempt['key'])
        self.assertEqual(row['sha256'], attempt['sha'])
        self.assertEqual(row['size_bytes'], 10)
        # ``attempts`` grew on the claim only and a parse failure never bumps version.        self.assertEqual(row['attempts'], 1)
        self.assertEqual(row['version'], 1)
        # A parse failure is not an upload failure: retry_upload refuses it.
        self.assertFalse(await self.h.repo.retry_upload(1, attempt['doc']['id']))

        self.assertFalse(
            await self.h.repo.fail_import(1, attempt['doc']['id'], str(uuid4()), error_code='c', error_message='m')
        )
        self.assertFalse(
            await self.h.repo.fail_import(2, attempt['doc']['id'], token, error_code='c', error_message='m')
        )
        bad_errors = (
            ('', 'm'),
            ('解析', 'm'),
            ('c' * (MAX_ERROR_CODE_LENGTH + 1), 'm'),
            ('c', 'm' * (MAX_ERROR_MESSAGE_LENGTH + 1)),
        )
        for code, message in bad_errors:
            with self.assertRaises(ValueError):
                await self.h.repo.fail_import(1, attempt['doc']['id'], token, error_code=code, error_message=message)
        self.assertEqual(self.h.document_row(attempt['doc']['id'])['error_code'], 'document.needs_ocr')

    async def test_retry_import_requeues_only_parse_failures(self) -> None:
        attempt = await self._queued()
        token = str(uuid4())
        await self.h.repo.claim_import(1, attempt['doc']['id'], 'worker-1', token)
        await self.h.repo.fail_import(
            1,
            attempt['doc']['id'],
            token,
            error_code='document.parse_failed',
            error_message='The document could not be parsed.',
        )
        self.assertTrue(await self.h.repo.retry_import(1, attempt['doc']['id']))
        row = self.h.document_row(attempt['doc']['id'])
        self.assertEqual(row['state'], 'queued')
        self.assertIsNone(row['error_code'])
        self.assertIsNone(row['error_message'])
        # Source, attempts and version are preserved.
        self.assertEqual(row['s3_key'], attempt['key'])
        self.assertEqual(row['sha256'], attempt['sha'])
        self.assertEqual(row['attempts'], 1)
        self.assertEqual(row['version'], 1)
        # The superseded lease is dead: neither publish nor failure goes through.
        self.assertFalse(
            await self.h.repo.complete_import(
                1, attempt['doc']['id'], token, page_count=1, chunks=[Chunk(1, 0, 'late')]
            )
        )
        self.assertFalse(
            await self.h.repo.fail_import(1, attempt['doc']['id'], token, error_code='c', error_message='m')
        )
        self.assertEqual(self.h.document_row(attempt['doc']['id'])['state'], 'queued')

    async def test_retry_import_never_revives_other_states(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Other')
        for state in ('pending_upload', 'uploading', 'queued', 'processing', 'ready', 'unsupported'):
            doc = await self.h.repo.create_document(1, base['id'], filename=f'{state}.pdf', mime_type='application/pdf')
            self.h.raw.execute(
                "UPDATE document SET state = ?, s3_key = 'k', sha256 = ?, size_bytes = 10 WHERE id = ?",
                (state, hashlib.sha256(state.encode()).hexdigest(), doc['id']),
            )
            self.h.raw.commit()
            self.assertFalse(await self.h.repo.retry_import(1, doc['id']), msg=state)
            self.assertEqual(self.h.document_row(doc['id'])['state'], state)

        gone = await self._queued(base=base)
        gone_token = str(uuid4())
        await self.h.repo.claim_import(1, gone['doc']['id'], 'worker-1', gone_token)
        await self.h.repo.fail_import(1, gone['doc']['id'], gone_token, error_code='c', error_message='m')
        self.assertTrue(await self.h.repo.delete_document(1, gone['doc']['id']))
        self.assertFalse(await self.h.repo.retry_import(1, gone['doc']['id']))
        self.assertEqual(self.h.document_row(gone['doc']['id'])['state'], 'deleted')

        # A missing source or an unsupported MIME is not a resumable parse failure.
        broken_sources = (
            ('sha256', None),
            ('s3_key', None),
            ('size_bytes', None),
            ('mime_type', 'text/csv'),
        )
        for column, value in broken_sources:
            broken = await self._queued(base=base)
            self.h.raw.execute(
                f"UPDATE document SET state = 'failed', {column} = ? WHERE id = ?",
                (value, broken['doc']['id']),
            )
            self.h.raw.commit()
            self.assertFalse(await self.h.repo.retry_import(1, broken['doc']['id']))
            self.assertEqual(self.h.document_row(broken['doc']['id'])['state'], 'failed')

        # An upload failure (no content hash yet) stays with retry_upload.
        upload_failed = await self.h.repo.create_document(1, base['id'], filename='up.pdf', mime_type='application/pdf')
        self.h.raw.execute("UPDATE document SET state = 'failed' WHERE id = ?", (upload_failed['id'],))
        self.h.raw.commit()
        self.assertFalse(await self.h.repo.retry_import(1, upload_failed['id']))
        self.assertTrue(await self.h.repo.retry_upload(1, upload_failed['id']))
        self.assertFalse(await self.h.repo.retry_import(1, str(uuid4())))

    async def test_late_import_does_not_revive_deleted_documents(self) -> None:
        attempt = await self._queued()
        token = str(uuid4())
        await self.h.repo.claim_import(1, attempt['doc']['id'], 'worker-1', token)
        self.assertTrue(await self.h.repo.delete_document(1, attempt['doc']['id']))
        chunks = [Chunk(1, 0, 'late')]
        self.assertFalse(await self.h.repo.complete_import(1, attempt['doc']['id'], token, page_count=1, chunks=chunks))
        self.assertFalse(
            await self.h.repo.fail_import(1, attempt['doc']['id'], token, error_code='c', error_message='m')
        )
        self.assertEqual(self.h.document_row(attempt['doc']['id'])['state'], 'deleted')
        self.assertEqual(self.h.count('SELECT COUNT(*) FROM document_chunk'), 0)

        parented = await self._queued()
        parent_token = str(uuid4())
        await self.h.repo.claim_import(1, parented['doc']['id'], 'worker-1', parent_token)
        self.assertTrue(await self.h.repo.delete_knowledge_base(1, parented['base']['id']))
        self.assertFalse(
            await self.h.repo.complete_import(1, parented['doc']['id'], parent_token, page_count=1, chunks=chunks)
        )
        self.assertEqual(self.h.document_row(parented['doc']['id'])['state'], 'deleted')
        self.assertEqual(self.h.count('SELECT COUNT(*) FROM document_chunk'), 0)


class ProcessingLeaseTest(unittest.IsolatedAsyncioTestCase):
    """The lease heartbeat and the expired-lease reaper, run as real SQL against SQLite.

    Coverage limits: only one connection exists here, so this proves the SQL
    predicates, the pre-SQL validation and the transaction boundaries. PostgreSQL
    READ COMMITTED re-evaluation (EPQ) after a concurrent heartbeat is not modelled
    and must be verified against a real server. The two fixed ``SET LOCAL``
    statements are recorded but not executed - SQLite has no session settings - so
    only their presence and order are asserted.
    """

    def setUp(self) -> None:
        self.h = _Harness()

    def tearDown(self) -> None:
        self.h.raw.close()

    async def _processing(
        self,
        *,
        owner: int = 1,
        base: dict | None = None,
        worker: str = 'worker-1',
        locked_at: str = '2020-01-01T00:00:00+00:00',
    ) -> dict:
        """One document taken to a claimed ``processing`` row via the repository."""
        if base is None:
            base = await self.h.repo.create_knowledge_base(owner, 'Papers')
        doc = await self.h.repo.create_document(owner, base['id'], filename='a.pdf', mime_type='application/pdf')
        token = str(uuid4())
        key = f'knowledge/{doc["id"]}/a.pdf'
        self.assertTrue(await self.h.repo.begin_upload(owner, doc['id'], token, s3_key=key))
        digest = hashlib.sha256(doc['id'].encode()).hexdigest()
        self.assertEqual(
            await self.h.repo.finish_upload(owner, doc['id'], token, size_bytes=10, sha256=digest),
            'queued',
        )
        self.assertIsNotNone(await self.h.repo.claim_import(owner, doc['id'], worker, token))
        # A fixed past timestamp makes the expiry boundary deterministic.
        self.h.raw.execute('UPDATE document SET locked_at = ? WHERE id = ?', (locked_at, doc['id']))
        self.h.raw.commit()
        return {'base': base, 'doc': doc, 'token': token, 'key': key, 'sha': digest, 'worker': worker}

    async def _state_row(
        self,
        base: dict,
        state: str,
        *,
        locked_by: str | None = 'worker-1',
        token: str | None = None,
        locked_at: str | None = '2020-01-01T00:00:00+00:00',
    ) -> dict:
        """A document forced into an arbitrary state/lease combination for the reaper."""
        doc = await self.h.repo.create_document(1, base['id'], filename=f'{state}.pdf')
        self.h.raw.execute(
            'UPDATE document SET state = ?, locked_by = ?, lease_token = ?, locked_at = ?, deleted_at = ? WHERE id = ?',
            (
                state,
                locked_by,
                token,
                locked_at,
                '2020-01-01T00:00:00+00:00' if state == 'deleted' else None,
                doc['id'],
            ),
        )
        self.h.raw.commit()
        return doc

    def _snapshot(self, document_id: str) -> dict:
        row = self.h.document_row(document_id)
        assert row is not None
        return dict(row)

    async def test_heartbeat_import_refreshes_only_locked_at(self) -> None:
        attempt = await self._processing()
        doc_id = attempt['doc']['id']
        self.h.seed_chunk(doc_id, 1, 0, 'kept text')
        baseline = self._snapshot(doc_id)

        start = len(self.h.executed)
        self.assertTrue(await self.h.repo.heartbeat_import(1, doc_id, attempt['token']))
        # The two fixed SET LOCAL statements come first (see
        # ``test_maintenance_limits_are_set_before_each_maintenance_update``); the
        # lease UPDATE is the statement that really touches the row.
        sql = next(statement for statement in self.h.executed[start:] if 'SET locked_at = %s' in statement)
        # The short UPDATE writes the lease timestamp and nothing else.
        self.assertIn('SET locked_at = %s', sql)
        for forbidden in ('updated_at', 'attempts', 'version', 'SET state', 's3_key ='):
            self.assertNotIn(forbidden, sql)
        self.assertIn("state = 'processing'", sql)
        self.assertIn('lease_token = %s', sql)
        self.assertIn('kb.user_id = %s', sql)

        row = self._snapshot(doc_id)
        # Only the lease timestamp moved: the heartbeat writes ``now`` instead of
        # the seeded 2020 value, while the business columns stay byte-identical.
        self.assertNotEqual(row['locked_at'], baseline['locked_at'])
        unchanged = (
            'state',
            'locked_by',
            'lease_token',
            'updated_at',
            'attempts',
            'version',
            's3_key',
            'sha256',
            'size_bytes',
            'error_code',
        )
        for column in unchanged:
            self.assertEqual(row[column], baseline[column], column)
        self.assertEqual(
            [item['text'] for item in await self.h.repo.list_chunks(1, doc_id)],
            ['kept text'],
        )

    async def test_heartbeat_import_misses_leave_the_row_untouched(self) -> None:
        attempt = await self._processing()
        doc_id = attempt['doc']['id']
        baseline = self._snapshot(doc_id)

        self.assertFalse(await self.h.repo.heartbeat_import(2, doc_id, attempt['token']))
        self.assertFalse(await self.h.repo.heartbeat_import(1, doc_id, str(uuid4())))
        self.assertFalse(await self.h.repo.heartbeat_import(1, str(uuid4()), attempt['token']))
        self.assertFalse(await self.h.repo.heartbeat_import(1, 'not-a-uuid', attempt['token']))
        self.assertFalse(await self.h.repo.heartbeat_import(1, doc_id, 'not-a-uuid'))
        self.assertEqual(self._snapshot(doc_id), baseline)

        # A queued (not yet claimed) row has no import lease to refresh.
        queued = await self.h.repo.create_document(1, attempt['base']['id'], filename='queued.pdf')
        self.assertFalse(await self.h.repo.heartbeat_import(1, queued['id'], str(uuid4())))

        # The reserved upload holder can never refresh an import lease, even with
        # the row's own current token.
        forged_token = str(uuid4())
        forged = await self._state_row(attempt['base'], 'processing', locked_by=UPLOAD_LOCK_OWNER, token=forged_token)
        self.assertFalse(await self.h.repo.heartbeat_import(1, forged['id'], forged_token))

        # Other states never refresh, even when a lease-shaped row survives.
        ready_token = str(uuid4())
        ready = await self._state_row(attempt['base'], 'ready', locked_by='worker-1', token=ready_token)
        self.assertFalse(await self.h.repo.heartbeat_import(1, ready['id'], ready_token))

        # A tombstoned document or a tombstoned parent never refreshes.
        self.assertTrue(await self.h.repo.delete_document(1, doc_id))
        self.assertFalse(await self.h.repo.heartbeat_import(1, doc_id, attempt['token']))
        parented = await self._processing()
        self.assertTrue(await self.h.repo.delete_knowledge_base(1, parented['base']['id']))
        self.assertFalse(await self.h.repo.heartbeat_import(1, parented['doc']['id'], parented['token']))

    async def test_heartbeat_import_renews_expired_lease_for_the_same_holder(self) -> None:
        attempt = await self._processing(locked_at='2020-01-01T00:00:00+00:00')
        self.assertTrue(await self.h.repo.heartbeat_import(1, attempt['doc']['id'], attempt['token']))
        self.assertTrue(await self.h.repo.heartbeat_import(1, attempt['doc']['id'], attempt['token']))
        row = self._snapshot(attempt['doc']['id'])
        self.assertEqual(row['state'], 'processing')
        self.assertEqual(row['locked_by'], 'worker-1')
        self.assertEqual(row['lease_token'], attempt['token'])
        self.assertNotEqual(row['locked_at'], '2020-01-01T00:00:00+00:00')

    async def test_requeue_expired_imports_boundary_states_and_source(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Papers')
        expired = await self._processing(base=base, locked_at='2020-01-01T00:00:00+00:00')
        boundary = await self._processing(base=base, locked_at='2021-01-01T00:00:00+00:00')
        fresh = await self._processing(base=base, locked_at='2999-01-01T00:00:00+00:00')
        self.h.seed_chunk(expired['doc']['id'], 1, 0, 'kept text')
        self.h.raw.execute(
            "UPDATE document SET error_code = 'document.import_failed', error_message = 'x' WHERE id = ?",
            (expired['doc']['id'],),
        )
        self.h.raw.commit()

        # Exactly the cutoff is not expired: the comparison is strict.
        self.assertEqual(await self.h.repo.requeue_expired_imports(locked_before=datetime(2021, 1, 1, tzinfo=UTC)), 1)
        row = self._snapshot(expired['doc']['id'])
        self.assertEqual(row['state'], 'queued')
        self.assertIsNone(row['locked_by'])
        self.assertIsNone(row['lease_token'])
        self.assertIsNone(row['locked_at'])
        self.assertIsNone(row['error_code'])
        self.assertIsNone(row['error_message'])
        # Source, chunks and counters survive the reclaim.
        self.assertEqual(row['s3_key'], expired['key'])
        self.assertEqual(row['sha256'], expired['sha'])
        self.assertEqual(row['size_bytes'], 10)
        self.assertEqual(row['attempts'], 1)
        self.assertEqual(row['version'], 1)
        self.assertEqual(
            [item['text'] for item in await self.h.repo.list_chunks(1, expired['doc']['id'])],
            ['kept text'],
        )
        for kept in (boundary, fresh):
            self.assertEqual(self._snapshot(kept['doc']['id'])['state'], 'processing')

        # A later run reclaims the boundary row but never the future one.
        self.assertEqual(await self.h.repo.requeue_expired_imports(locked_before=datetime(2022, 1, 1, tzinfo=UTC)), 1)
        self.assertEqual(self._snapshot(boundary['doc']['id'])['state'], 'queued')
        self.assertEqual(self._snapshot(fresh['doc']['id'])['state'], 'processing')

    async def test_requeue_expired_imports_limit_and_deterministic_order(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Papers')
        first = await self._processing(base=base, locked_at='2020-01-01T00:00:00+00:00')
        second = await self._processing(base=base, locked_at='2020-01-02T00:00:00+00:00')
        third = await self._processing(base=base, locked_at='2020-01-03T00:00:00+00:00')

        cutoff = datetime(2021, 1, 1, tzinfo=UTC)
        self.assertEqual(await self.h.repo.requeue_expired_imports(locked_before=cutoff, limit=2), 2)
        self.assertEqual(self._snapshot(first['doc']['id'])['state'], 'queued')
        self.assertEqual(self._snapshot(second['doc']['id'])['state'], 'queued')
        self.assertEqual(self._snapshot(third['doc']['id'])['state'], 'processing')
        # A bounded limit clamps rather than scanning the whole table.
        self.assertEqual(await self.h.repo.requeue_expired_imports(locked_before=cutoff, limit=10**9), 1)
        self.assertEqual(self._snapshot(third['doc']['id'])['state'], 'queued')

        # Equal timestamps break the tie by id, so the choice stays stable.
        ties = [await self._processing(base=base, locked_at='2019-01-01T00:00:00+00:00') for _ in range(2)]
        self.assertEqual(await self.h.repo.requeue_expired_imports(locked_before=cutoff, limit=1), 1)
        ordered = sorted(ties, key=lambda item: item['doc']['id'])
        self.assertEqual(self._snapshot(ordered[0]['doc']['id'])['state'], 'queued')
        self.assertEqual(self._snapshot(ordered[1]['doc']['id'])['state'], 'processing')

    async def test_requeue_expired_imports_ignores_other_states_and_holders(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Papers')
        token = str(uuid4())
        noise = {
            'uploading': await self._state_row(base, 'uploading', locked_by=UPLOAD_LOCK_OWNER, token=token),
            'ready': await self._state_row(base, 'ready', locked_by=None, token=None),
            'failed': await self._state_row(base, 'failed', locked_by=None, token=None),
            'unsupported': await self._state_row(base, 'unsupported', locked_by=None, token=None),
            'deleted': await self._state_row(base, 'deleted', locked_by=None, token=None, locked_at=None),
            'queued': await self._state_row(base, 'queued', locked_by=None, token=None),
        }
        # A processing row must have a non-upload holder, a token and a timestamp.
        no_holder = await self._state_row(base, 'processing', locked_by=None, token=token)
        no_token = await self._state_row(base, 'processing', locked_by='worker-1', token=None)
        no_time = await self._state_row(base, 'processing', locked_by='worker-1', token=token, locked_at=None)
        uploader = await self._state_row(base, 'processing', locked_by=UPLOAD_LOCK_OWNER, token=token)

        # A processing row whose parent base is tombstoned is never reclaimed, even
        # though the document row itself was forced back to a lease-shaped state.
        orphan_base = await self.h.repo.create_knowledge_base(1, 'Orphan')
        orphan = await self._state_row(orphan_base, 'processing', locked_by='worker-1', token=str(uuid4()))
        self.assertTrue(await self.h.repo.delete_knowledge_base(1, orphan_base['id']))
        self.h.raw.execute(
            "UPDATE document SET state = 'processing', deleted_at = NULL, locked_by = 'worker-1',"
            " lease_token = ?, locked_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (str(uuid4()), orphan['id']),
        )
        self.h.raw.commit()

        cutoff = datetime(2021, 1, 1, tzinfo=UTC)
        self.assertEqual(await self.h.repo.requeue_expired_imports(locked_before=cutoff), 0)
        for state, doc in noise.items():
            self.assertEqual(self._snapshot(doc['id'])['state'], state)
        for doc in (no_holder, no_token, no_time, uploader, orphan):
            self.assertEqual(self._snapshot(doc['id'])['state'], 'processing')

    def test_reaper_is_internal_and_needs_no_owner(self) -> None:
        # Global maintenance: there is no ``user_id`` parameter that an HTTP layer
        # could route, and the cutoff is keyword-only and required.
        signature = inspect.signature(self.h.repo.requeue_expired_imports)
        self.assertEqual(list(signature.parameters), ['locked_before', 'limit'])
        self.assertEqual(signature.parameters['locked_before'].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(signature.parameters['locked_before'].default, inspect.Parameter.empty)
        self.assertEqual(signature.parameters['limit'].default, DEFAULT_PAGE_SIZE)

    async def test_requeue_expired_imports_rejects_a_bad_cutoff_before_sql(self) -> None:
        await self._processing()
        start = len(self.h.executed)
        for bad in (datetime(2020, 1, 1), '2020-01-01T00:00:00+00:00', None, 123, True):
            with self.assertRaises(ValueError, msg=f'accepted locked_before={bad!r}'):
                await self.h.repo.requeue_expired_imports(locked_before=bad)  # type: ignore[arg-type]
        # No statement was issued for any rejected cutoff.
        self.assertEqual(len(self.h.executed), start)

    async def test_requeue_expired_imports_normalizes_an_offset_aware_cutoff(self) -> None:
        attempt = await self._processing(locked_at='2020-01-01T00:00:00+00:00')
        # 2019-12-31T19:00-05:00 is exactly 2020-01-01T00:00Z, so it is not expired.
        same_instant = datetime(2019, 12, 31, 19, 0, tzinfo=timezone(timedelta(hours=-5)))
        self.assertEqual(await self.h.repo.requeue_expired_imports(locked_before=same_instant), 0)
        self.assertEqual(self._snapshot(attempt['doc']['id'])['state'], 'processing')
        # One second after that instant the lease really is expired.
        self.assertEqual(
            await self.h.repo.requeue_expired_imports(locked_before=datetime(2020, 1, 1, 0, 0, 1, tzinfo=UTC)), 1
        )
        self.assertEqual(self._snapshot(attempt['doc']['id'])['state'], 'queued')

    async def test_requeue_expired_imports_rolls_back_with_the_outer_transaction(self) -> None:
        attempt = await self._processing(locked_at='2020-01-01T00:00:00+00:00')
        self.h.raw.execute('BEGIN')
        self.assertEqual(await self.h.repo.requeue_expired_imports(locked_before=datetime(2021, 1, 1, tzinfo=UTC)), 1)
        await self.h.outer_transaction()
        row = self._snapshot(attempt['doc']['id'])
        self.assertEqual(row['state'], 'processing')
        self.assertEqual(row['locked_by'], 'worker-1')
        self.assertEqual(row['lease_token'], attempt['token'])

    async def test_requeue_invalidates_a_late_lease_and_allows_a_new_claim(self) -> None:
        attempt = await self._processing(locked_at='2020-01-01T00:00:00+00:00')
        doc_id = attempt['doc']['id']
        old_token = attempt['token']
        self.assertEqual(await self.h.repo.requeue_expired_imports(locked_before=datetime(2021, 1, 1, tzinfo=UTC)), 1)
        # The reclaimed lease is dead: neither publish, failure nor heartbeat goes through.
        self.assertFalse(await self.h.repo.heartbeat_import(1, doc_id, old_token))
        self.assertFalse(
            await self.h.repo.complete_import(1, doc_id, old_token, page_count=1, chunks=[Chunk(1, 0, 'late')])
        )
        self.assertFalse(await self.h.repo.fail_import(1, doc_id, old_token, error_code='c', error_message='m'))
        self.assertEqual(self._snapshot(doc_id)['state'], 'queued')

        # A fresh claim works, and the superseded late holder still cannot publish.
        new_token = str(uuid4())
        self.assertIsNotNone(await self.h.repo.claim_import(1, doc_id, 'worker-2', new_token))
        self.assertFalse(
            await self.h.repo.complete_import(1, doc_id, old_token, page_count=1, chunks=[Chunk(1, 0, 'late')])
        )
        self.assertFalse(await self.h.repo.fail_import(1, doc_id, old_token, error_code='c', error_message='m'))
        self.assertFalse(await self.h.repo.heartbeat_import(1, doc_id, old_token))
        self.assertTrue(await self.h.repo.heartbeat_import(1, doc_id, new_token))
        self.assertTrue(
            await self.h.repo.complete_import(1, doc_id, new_token, page_count=1, chunks=[Chunk(1, 0, 'new')])
        )

    async def test_requeue_sql_rechecks_expiry_outside_the_candidate_snapshot(self) -> None:
        attempt = await self._processing(locked_at='2020-01-01T00:00:00+00:00')
        start = len(self.h.executed)
        await self.h.repo.requeue_expired_imports(locked_before=datetime(2021, 1, 1, tzinfo=UTC))
        sql = next(stmt for stmt in self.h.executed[start:] if 'WITH expired AS' in stmt)
        self.assertIn('ORDER BY d.locked_at ASC, d.id ASC', sql)
        self.assertIn('LIMIT %s', sql)
        # The candidate list is only a bounded snapshot; the outer UPDATE repeats
        # every predicate, so a heartbeat that committed while this waited on the
        # row lock is caught by the second check instead of being reclaimed.
        outer = sql[sql.index('WHERE id IN (SELECT id FROM expired)') :]
        self.assertIn("state = 'processing'", outer)
        self.assertIn('deleted_at IS NULL', outer)
        self.assertIn('locked_by IS NOT NULL', outer)
        self.assertIn('locked_by <> %s', outer)
        self.assertIn('lease_token IS NOT NULL', outer)
        self.assertIn('locked_at < %s', outer)
        self.assertIn('kb.deleted_at IS NULL', outer)
        self.assertNotIn('SKIP LOCKED', sql)
        self.assertNotIn('FOR UPDATE', sql)
        self.assertEqual(self._snapshot(attempt['doc']['id'])['state'], 'queued')

    def test_queued_import_partial_index_shape(self) -> None:
        row = self.h.raw.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_document_queued_import'"
        ).fetchone()
        self.assertIsNotNone(row)
        sql = ' '.join(row[0].split())
        self.assertIn('ON document (created_at, id)', sql)
        self.assertIn("WHERE state = 'queued' AND deleted_at IS NULL", sql)
        listed = [
            item
            for item in self.h.raw.execute("PRAGMA index_list('document')").fetchall()
            if item[1] == 'idx_document_queued_import'
        ]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0][4], 1)  # SQLite's partial-index flag

    async def test_maintenance_limits_are_set_before_each_maintenance_update(self) -> None:
        """Both maintenance writes send the two fixed limits first, in order.

        SQLite has no session settings, so this asserts only that the two exact
        statements were sent, once each and before the maintenance UPDATE. That
        the server really applies the 5s/10s values, and that the caller's
        commit/rollback discards them, is PostgreSQL behaviour and is not
        verified here.
        """
        attempt = await self._processing(locked_at='2020-01-01T00:00:00+00:00')

        start = len(self.h.executed)
        self.assertTrue(await self.h.repo.heartbeat_import(1, attempt['doc']['id'], attempt['token']))
        issued = self.h.executed[start:]
        self.assertEqual(issued[:2], [LOCK_TIMEOUT_SQL, STATEMENT_TIMEOUT_SQL])
        self.assertIn('SET locked_at = %s', issued[2])
        self.assertEqual(len(issued), 3)
        self.assertEqual(issued.count(LOCK_TIMEOUT_SQL), 1)
        self.assertEqual(issued.count(STATEMENT_TIMEOUT_SQL), 1)
        self.assertNotEqual(self._snapshot(attempt['doc']['id'])['locked_at'], '2020-01-01T00:00:00+00:00')

        expired = await self._processing(base=attempt['base'], locked_at='2020-01-01T00:00:00+00:00')
        start = len(self.h.executed)
        self.assertEqual(await self.h.repo.requeue_expired_imports(locked_before=datetime(2021, 1, 1, tzinfo=UTC)), 1)
        issued = self.h.executed[start:]
        self.assertEqual(issued[:2], [LOCK_TIMEOUT_SQL, STATEMENT_TIMEOUT_SQL])
        self.assertIn('WITH expired AS', issued[2])
        self.assertEqual(len(issued), 3)
        self.assertEqual(self._snapshot(expired['doc']['id'])['state'], 'queued')

    async def test_limits_are_set_only_by_the_two_maintenance_methods(self) -> None:
        """No other repository method sets a session timeout.

        Two-sided: the live calls show ordinary import SQL still runs without any
        ``SET LOCAL``, and the source scan shows only the heartbeat and the reaper
        mention one at all.
        """
        attempt = await self._processing(locked_at='2020-01-01T00:00:00+00:00')
        doc_id = attempt['doc']['id']
        self.h.seed_chunk(doc_id, 1, 0, 'text')

        start = len(self.h.executed)
        await self.h.repo.list_queued_imports()
        await self.h.repo.get_document(1, doc_id)
        await self.h.repo.list_chunks(1, doc_id)
        await self.h.repo.fail_import(1, doc_id, attempt['token'], error_code='c', error_message='m')
        await self.h.repo.retry_import(1, doc_id)
        self.assertFalse(
            await self.h.repo.complete_import(1, doc_id, attempt['token'], page_count=1, chunks=[Chunk(1, 0, 'x')])
        )
        for statement in self.h.executed[start:]:
            self.assertNotIn('SET LOCAL', statement)

        setters = sorted(
            name
            for name, method in inspect.getmembers(KnowledgeRepository, inspect.isfunction)
            if 'SET LOCAL' in inspect.getsource(method)
        )
        self.assertEqual(setters, ['heartbeat_import', 'requeue_expired_imports'])

    async def test_rejected_maintenance_inputs_reach_no_sql_and_no_limits(self) -> None:
        attempt = await self._processing()
        doc_id = attempt['doc']['id']
        baseline = self._snapshot(doc_id)
        start = len(self.h.executed)

        self.assertFalse(await self.h.repo.heartbeat_import(1, 'not-a-uuid', attempt['token']))
        self.assertFalse(await self.h.repo.heartbeat_import(1, doc_id, 'not-a-uuid'))
        self.assertFalse(await self.h.repo.heartbeat_import(1, None, None))
        for bad in (datetime(2020, 1, 1), '2020-01-01T00:00:00+00:00', None, 7, True):
            with self.assertRaises(ValueError, msg=f'accepted locked_before={bad!r}'):
                await self.h.repo.requeue_expired_imports(locked_before=bad)  # type: ignore[arg-type]

        # No statement was issued, so the two fixed limits were not set either.
        self.assertEqual(self.h.executed[start:], [])
        self.assertEqual(self._snapshot(doc_id), baseline)

    async def test_harness_noops_only_the_two_exact_statements(self) -> None:
        """A differently spelled session setting is not ignored by the double.

        Only the two recorded statements are skipped; any other ``SET`` really
        runs against SQLite and fails there, so the harness cannot hide a wrong
        setting.
        """
        async with self.h.connection.cursor() as cur:
            with self.assertRaises(sqlite3.OperationalError):
                await cur.execute("SET LOCAL lock_timeout = '6s'")
            with self.assertRaises(sqlite3.OperationalError):
                await cur.execute('SET statement_timeout = %s', ('10s',))


class CursorRowcountAdapterTest(unittest.IsolatedAsyncioTestCase):
    """The SQLite double must answer PostgreSQL's count for a data-modifying CTE.

    SQLite reports ``rowcount == -1`` for ``WITH ... UPDATE`` even though rows
    changed; PostgreSQL returns the real number. The adapter measures
    ``connection.total_changes`` for that statement shape only, and these checks
    run real SQL to show the fallback does not change plain DML or query counts.
    """

    def setUp(self) -> None:
        self.h = _Harness()
        self.h.raw.execute('CREATE TABLE counter (value INTEGER NOT NULL)')
        self.h.raw.executemany('INSERT INTO counter (value) VALUES (?)', [(1,), (2,), (3,)])
        self.h.raw.commit()

    def tearDown(self) -> None:
        self.h.raw.close()

    async def test_cte_update_rowcount_is_the_measured_delta(self) -> None:
        async with self.h.connection.cursor() as cur:
            # The native count of a plain UPDATE is kept as it is.
            await cur.execute('UPDATE counter SET value = value + 0')
            self.assertEqual(cur.rowcount, 3)

            # A CTE UPDATE with no result set reports the rows it really changed.
            await cur.execute('WITH x AS (SELECT 1) UPDATE counter SET value = value + 1')
            self.assertEqual(cur.rowcount, 3)

            # Zero matches is a real zero, not SQLite's -1.
            await cur.execute('WITH x AS (SELECT 1) UPDATE counter SET value = value + 1 WHERE value < 0')
            self.assertEqual(cur.rowcount, 0)

            # A CTE with a result set is a query; the adapter adds nothing.
            await cur.execute('WITH x AS (SELECT 1 AS n) SELECT n FROM x')
            self.assertEqual(cur.rowcount, -1)
            # The rows themselves prove what the counted statements changed: the
            # +0 left the values alone and the one matching CTE UPDATE added one.
            await cur.execute('SELECT value FROM counter ORDER BY value')
            self.assertEqual([row['value'] for row in await cur.fetchall()], [2, 3, 4])


class LockingSqlTest(unittest.IsolatedAsyncioTestCase):
    """Which statements take row locks.

    SQLite cannot model locks, so this asserts on the statement text the
    repository actually sent (captured before the SQLite-only rewrite). It is not
    a concurrency proof and says nothing about real PostgreSQL behaviour.
    """

    def setUp(self) -> None:
        self.h = _Harness()

    def tearDown(self) -> None:
        self.h.raw.close()

    async def test_read_and_rename_paths_take_no_locks(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Mine')
        doc = await self.h.repo.create_document(1, base['id'], filename='x.pdf')
        self.h.seed_chunk(doc['id'], 1, 0, 'text')
        start = len(self.h.executed)
        await self.h.repo.list_knowledge_bases(1)
        await self.h.repo.get_knowledge_base(1, base['id'])
        await self.h.repo.rename_knowledge_base(1, base['id'], 'Renamed')
        await self.h.repo.list_documents(1, base['id'])
        await self.h.repo.get_document(1, doc['id'])
        await self.h.repo.list_chunks(1, doc['id'])
        issued = self.h.executed[start:]
        self.assertEqual(len(issued), 6)
        for sql in issued:
            self.assertNotIn('FOR SHARE', sql)
            self.assertNotIn('FOR UPDATE', sql)

    async def test_import_paths_take_no_row_locks_and_recheck_owner(self) -> None:
        async def queued() -> dict:
            base = await self.h.repo.create_knowledge_base(1, 'Papers')
            doc = await self.h.repo.create_document(1, base['id'], filename='a.pdf', mime_type='application/pdf')
            token = str(uuid4())
            await self.h.repo.begin_upload(1, doc['id'], token, s3_key=f'knowledge/{doc["id"]}.pdf')
            digest = hashlib.sha256(doc['id'].encode()).hexdigest()
            self.assertEqual(
                await self.h.repo.finish_upload(1, doc['id'], token, size_bytes=10, sha256=digest),
                'queued',
            )
            return {'doc': doc, 'sha': digest}

        # Explicit document CAS only: no SKIP LOCKED, no row locks, and every
        # write still carries the owner predicate rather than trusting a list.
        attempt = await queued()
        token = str(uuid4())
        start = len(self.h.executed)
        await self.h.repo.list_queued_imports()
        listed = self.h.executed[start]
        self.assertIn("d.state = 'queued'", listed)
        self.assertIn('kb.deleted_at IS NULL', listed)
        self.assertNotIn('SKIP LOCKED', listed)
        self.assertNotIn('FOR UPDATE', listed)

        start = len(self.h.executed)
        self.assertIsNotNone(await self.h.repo.claim_import(1, attempt['doc']['id'], 'worker-1', token))
        claimed = self.h.executed[start]
        self.assertIn("state = 'queued'", claimed)
        self.assertIn('lease_token IS NULL', claimed)
        self.assertIn('kb.user_id = %s', claimed)
        self.assertIn('kb.deleted_at IS NULL', claimed)
        self.assertNotIn('SKIP LOCKED', claimed)

        start = len(self.h.executed)
        self.assertTrue(
            await self.h.repo.complete_import(
                1, attempt['doc']['id'], token, page_count=1, chunks=[Chunk(1, 0, 'text')]
            )
        )
        publish = self.h.executed[start]
        self.assertIn("state = 'processing'", publish)
        self.assertIn('lease_token = %s', publish)
        self.assertIn('version = version + 1', publish)
        self.assertIn('locked_by <> %s', publish)
        self.assertIn('kb.user_id = %s', publish)
        self.assertIn('DELETE FROM document_chunk', self.h.executed[start + 1])

        rerun = await queued()
        rerun_token = str(uuid4())
        self.assertIsNotNone(await self.h.repo.claim_import(1, rerun['doc']['id'], 'worker-1', rerun_token))
        start = len(self.h.executed)
        self.assertTrue(
            await self.h.repo.fail_import(1, rerun['doc']['id'], rerun_token, error_code='c', error_message='m')
        )
        failure = self.h.executed[start]
        self.assertIn("state = 'failed'", failure)
        self.assertIn('kb.user_id = %s', failure)
        # The uploaded source and the counters are preserved, not rewritten.
        for column in ('s3_key', 'sha256', 'size_bytes'):
            self.assertNotIn(column, failure)
        self.assertNotIn('version', failure)
        self.assertNotIn('attempts', failure)

        start = len(self.h.executed)
        self.assertTrue(await self.h.repo.retry_import(1, rerun['doc']['id']))
        retry = self.h.executed[start]
        self.assertIn("state = 'queued'", retry)
        # The source columns are only read (WHERE) and never assigned.
        for column in ('s3_key', 'sha256', 'size_bytes'):
            self.assertNotIn(f'{column} =', retry)
        self.assertNotIn('version', retry)
        self.assertIn('kb.user_id = %s', retry)

    async def test_delete_paths_do_not_use_for_key_share(self) -> None:
        base = await self.h.repo.create_knowledge_base(1, 'Mine')
        doc = await self.h.repo.create_document(1, base['id'], filename='x.pdf')
        start = len(self.h.executed)
        self.assertTrue(await self.h.repo.delete_document(1, doc['id']))
        self.assertTrue(await self.h.repo.delete_knowledge_base(1, base['id']))
        for sql in self.h.executed[start:]:
            self.assertNotIn('FOR KEY SHARE', sql)

    def test_full_text_query_takes_no_locks(self) -> None:
        sql, _ = chunk_search_query(7, 'term', knowledge_base_id='kb-1', document_id='doc-1')
        self.assertNotIn('FOR SHARE', sql)
        self.assertNotIn('FOR UPDATE', sql)


class ChunkSearchQueryTest(unittest.TestCase):
    """Static shape of the PostgreSQL full-text query - NOT run against real PG."""

    def test_search_always_filters_by_owner_liveness_and_readiness(self) -> None:
        sql, params = chunk_search_query(7, 'term')
        self.assertIn('kb.user_id = %s', sql)
        self.assertIn('kb.deleted_at IS NULL', sql)
        self.assertIn('d.deleted_at IS NULL', sql)
        self.assertIn("d.state = 'ready'", sql)
        self.assertIn("plainto_tsquery('jiebaqry', %s)", sql)
        # user_id leads, the query text brackets the optional filters, then the
        # page bound and the offset; the default offset is a real bound parameter.
        self.assertEqual(params, [7, 'term', 'term', DEFAULT_PAGE_SIZE, 0])

    def test_search_selects_version_and_a_stable_tiebreaker(self) -> None:
        sql, _ = chunk_search_query(7, 'term')
        # The version travels with the chunk from the same statement/snapshot.
        self.assertIn('d.version', sql)
        # rank DESC, created_at DESC, then the id tiebreaker before page/ordinal,
        # so two equally-ranked rows cannot swap between pages.
        normalized = ' '.join(sql.split())
        self.assertIn(
            "ORDER BY ts_rank(c.search_vector, plainto_tsquery('jiebaqry', %s)) DESC,"
            ' d.created_at DESC, d.id ASC, c.page_no ASC, c.ordinal ASC',
            normalized,
        )
        # limit and offset are bound, never spliced into the statement.
        self.assertIn('LIMIT %s OFFSET %s', normalized)

    def test_search_does_not_truncate_the_indexed_text(self) -> None:
        sql, _ = chunk_search_query(7, 'term')
        # The article path truncates with SUBSTRING(... FOR 50000); chunks must not.
        self.assertNotIn('SUBSTRING', sql)
        self.assertNotIn('FOR 50000', sql)

    def test_optional_filters_are_parameterized_in_order(self) -> None:
        sql, params = chunk_search_query(7, 'term', knowledge_base_id='kb-1', document_id='doc-1', limit=5)
        self.assertIn('d.knowledge_base_id = %s', sql)
        self.assertIn('d.id = %s', sql)
        self.assertEqual(params, [7, 'term', 'kb-1', 'doc-1', 'term', 5, 0])
        # The caller still cannot escape its own rows.
        self.assertIn('kb.user_id = %s', sql)

    def test_search_limit_and_offset_are_bounded(self) -> None:
        _, params = chunk_search_query(7, 'term', limit=10**9, offset=-5)
        # The limit is clamped to the page cap; a negative offset floors at zero.
        self.assertEqual(params[-2:], [MAX_PAGE_SIZE, 0])
        _, paged = chunk_search_query(7, 'term', limit=5, offset=10)
        self.assertEqual(paged[-2:], [5, 10])
        # PostgreSQL ``OFFSET`` is a signed int8: anything larger is clamped to the
        # constant instead of being bound as a value the server would reject.
        _, huge = chunk_search_query(7, 'term', offset=2**63)
        self.assertEqual(huge[-1], MAX_OFFSET)
        _, exact = chunk_search_query(7, 'term', offset=MAX_OFFSET)
        self.assertEqual(exact[-1], MAX_OFFSET)


class KnowledgeSchemaTest(unittest.TestCase):
    """Static checks of PostgreSQL-only DDL - NOT executed against real PG."""

    def setUp(self) -> None:
        schema = SCHEMA_PATH.read_text(encoding='utf-8')
        marker = 'Private knowledge base'
        self.assertIn(marker, schema)
        self.section = schema[schema.index(marker) :]

    def test_tables_and_ownership_columns(self) -> None:
        for table in ('knowledge_base', 'document', 'document_chunk'):
            self.assertIn(f'CREATE TABLE IF NOT EXISTS {table}', self.section)
        # Ownership lives on the base only; documents must not carry a second owner.
        self.assertIn('knowledge_base_id UUID NOT NULL REFERENCES knowledge_base(id)', self.section)
        self.assertEqual(self.section.count('user_id INTEGER NOT NULL REFERENCES users(id)'), 1)
        self.assertIn('PRIMARY KEY (document_id, page_no, ordinal)', self.section)
        # No duplicate page table and no second full-text index.
        self.assertNotIn('document_page', self.section)
        self.assertEqual(self.section.count('USING GIN'), 1)

    def test_partial_unique_index_for_live_sha256(self) -> None:
        self.assertIn('CREATE UNIQUE INDEX IF NOT EXISTS idx_document_active_sha256', self.section)
        self.assertIn('WHERE sha256 IS NOT NULL AND deleted_at IS NULL', self.section)

    def test_document_state_and_numeric_checks(self) -> None:
        self.assertIn("state IN ('pending_upload', 'uploading', 'queued', 'processing',", self.section)
        self.assertIn("'ready', 'failed', 'unsupported', 'deleted')", self.section)
        self.assertIn('attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0)', self.section)
        self.assertIn('version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1)', self.section)
        self.assertIn('CHECK (size_bytes IS NULL OR size_bytes >= 0)', self.section)
        self.assertIn('CHECK (page_count IS NULL OR page_count >= 0)', self.section)
        self.assertIn("CHECK ((deleted_at IS NOT NULL) = (state = 'deleted'))", self.section)

    def test_partial_index_for_queued_imports(self) -> None:
        self.assertIn('CREATE INDEX IF NOT EXISTS idx_document_queued_import', self.section)
        self.assertIn('ON document (created_at, id)', self.section)
        self.assertIn("WHERE state = 'queued' AND deleted_at IS NULL;", self.section)
        # The lease fields now have a consumer: the uploader and the import worker.
        self.assertNotIn('have no consumer yet', self.section)

    def test_chunk_search_vector_indexes_full_text(self) -> None:
        generated = "search_vector tsvector GENERATED ALWAYS AS (to_tsvector('jiebaqry', text)) STORED"
        self.assertIn(generated, self.section)
        # The article path truncates with SUBSTRING(... FOR 50000); chunks must index all of it.
        self.assertNotIn('SUBSTRING', self.section)
        self.assertNotIn('FOR 50000', self.section)

    def test_new_sql_comments_are_english(self) -> None:
        # The project keeps source and comments in English; the legacy schema above
        # this section is Chinese and stays untouched.
        for line in self.section.splitlines():
            if line.lstrip().startswith('--'):
                self.assertFalse(
                    any('\u4e00' <= char <= '\u9fff' for char in line),
                    f'non-English comment in new schema section: {line}',
                )

    def test_schema_version_is_bumped_in_this_worktree(self) -> None:
        from hippo import storage

        self.assertEqual(storage.SCHEMA_VERSION, '31')


if __name__ == '__main__':
    unittest.main()
