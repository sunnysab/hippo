"""Owner-scoped storage for private knowledge bases, documents and chunks."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from ..knowledge.parse import MAX_CHUNK_CHARS, MAX_CHUNKS, MAX_PDF_PAGES, MAX_TEXT_CHARS, Chunk
from ..utils import to_utc_dt, utc_now_dt

#: Names and free-text parameters are trimmed and bounded before they reach SQL.
MAX_NAME_LENGTH = 200
MAX_FILENAME_LENGTH = 255
MAX_QUERY_LENGTH = 500

DEFAULT_PAGE_SIZE = 50
#: A caller cannot ask for the whole table in one page.
MAX_PAGE_SIZE = 200

#: PostgreSQL ``OFFSET`` takes a signed 64-bit integer; anything larger is a
#: database error rather than an empty page, so callers are clamped server-side.
MAX_OFFSET = 2**63 - 1

#: An upload attempt borrows ``document.lease_token``/``locked_by``; this label
#: marks the holder as the uploader rather than the import worker.
UPLOAD_LOCK_OWNER = 'upload'

#: The only constraint ``finish_upload`` turns into a dedupe answer.
ACTIVE_SHA_CONSTRAINT = 'idx_document_active_sha256'

#: Savepoint around the finishing UPDATE: a live-SHA conflict must roll back only
#: that statement and leave the caller's outer transaction usable.
FINISH_SAVEPOINT = 'knowledge_upload_finish'

#: Developer-facing upload errors are bounded ASCII, never client free text.
MAX_ERROR_CODE_LENGTH = 100
MAX_ERROR_MESSAGE_LENGTH = 500

#: Worker queue candidates: the metadata a job needs, and the MIME types the
#: parser accepts (kept in step with ``knowledge.parse.parse_document``).
IMPORT_MIME_TYPES = ('application/pdf', 'text/plain')
#: A worker label is bounded ASCII; the uploader's reserved label is not a worker.
MAX_WORKER_ID_LENGTH = 100
#: Chunks are inserted in batches so a 50000-row document is not one round trip.
IMPORT_CHUNK_BATCH_SIZE = 256

_HEX_DIGITS = frozenset('0123456789abcdef')


def _clean_text(value: str, *, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f'{field} must be a string.')
    trimmed = value.strip()
    if not trimmed:
        raise ValueError(f'{field} cannot be empty.')
    if len(trimmed) > max_length:
        raise ValueError(f'{field} is too long (max {max_length} characters).')
    return trimmed


def _as_uuid(value: Any) -> str | None:
    """Canonical UUID text, or ``None`` when ``value`` is not a UUID.

    Ids are identifiers, not permissions: a malformed one is simply not found, so
    the HTTP layer maps it to 404 instead of surfacing a database cast error.
    """
    try:
        return str(UUID(str(value)))
    except AttributeError, TypeError, ValueError:
        return None


def _bounded(limit: int, *, default: int = DEFAULT_PAGE_SIZE) -> int:
    """Clamp a caller-supplied page size: never unbounded, never below one row."""
    try:
        requested = int(limit)
    except TypeError, ValueError:
        return default
    if requested <= 0:
        return default
    return min(requested, MAX_PAGE_SIZE)


def _offset(offset: int) -> int:
    """A usable ``OFFSET``: a non-negative int, clamped to the signed int8 bound."""
    try:
        return min(max(int(offset), 0), MAX_OFFSET)
    except TypeError, ValueError:
        return 0


def _clean_upload_key(s3_key: Any) -> str:
    """The server-built object key for one attempt, or ``ValueError``.

    The key is internal and never comes from an HTTP client. Blank or padded
    input is rejected instead of being journalled as a key that later cleanup
    would treat as real.
    """
    if not isinstance(s3_key, str) or not s3_key or s3_key != s3_key.strip():
        raise ValueError('s3_key must be a non-empty internal object key.')
    return s3_key


def _upload_size(size_bytes: Any) -> int:
    """The real byte count of a stored document: a positive plain integer."""
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0:
        raise ValueError('size_bytes must be a positive integer.')
    return size_bytes


def _upload_sha256(sha256: Any) -> str:
    """The lower-case 64-character hex digest computed from the stored bytes."""
    if not isinstance(sha256, str) or len(sha256) != 64 or not set(sha256) <= _HEX_DIGITS:
        raise ValueError('sha256 must be a 64-character lower-case hex digest.')
    return sha256


def _clean_ascii(value: Any, *, field: str, max_length: int) -> str:
    """Trim and bound one developer-facing ASCII parameter."""
    if not isinstance(value, str):
        raise ValueError(f'{field} must be a string.')
    trimmed = value.strip()
    if not trimmed:
        raise ValueError(f'{field} cannot be empty.')
    if len(trimmed) > max_length:
        raise ValueError(f'{field} is too long (max {max_length} characters).')
    if not trimmed.isascii():
        raise ValueError(f'{field} must be ASCII.')
    return trimmed


def _import_page_count(page_count: Any) -> int:
    """The declared page count of a published document: a positive plain integer."""
    if isinstance(page_count, bool) or not isinstance(page_count, int):
        raise ValueError('page_count must be a positive integer.')
    if page_count < 1 or page_count > MAX_PDF_PAGES:
        raise ValueError(f'page_count must be between 1 and {MAX_PDF_PAGES}.')
    return page_count


def _import_chunk_rows(document_id: str, page_count: int, chunks: Any) -> list[tuple[Any, ...]]:
    """Validate one parse result and shape the ``document_chunk`` insert rows.

    Every check runs before the caller issues any SQL: plain-integer page and
    ordinal with ``(page_no, ordinal)`` strictly increasing, non-empty NUL-free
    text of at most ``MAX_CHUNK_CHARS``, and the whole-document caps the parser
    itself enforces. Duplicate text and pages without chunks are legal.
    """
    if isinstance(chunks, (str, bytes)) or not isinstance(chunks, Sequence):
        raise ValueError('chunks must be a sequence of parse.Chunk rows.')
    if not 1 <= len(chunks) <= MAX_CHUNKS:
        raise ValueError(f'chunks must hold between 1 and {MAX_CHUNKS} rows.')
    rows: list[tuple[Any, ...]] = []
    total = 0
    previous_page = -1
    previous_ordinal = -1
    for chunk in chunks:
        if not isinstance(chunk, Chunk):
            raise ValueError('chunks must contain parse.Chunk rows.')
        page_no = chunk.page_no
        ordinal = chunk.ordinal
        if isinstance(page_no, bool) or not isinstance(page_no, int) or not 1 <= page_no <= page_count:
            raise ValueError(f'chunk page_no must be an integer between 1 and {page_count}.')
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise ValueError('chunk ordinal must be a non-negative integer.')
        if (page_no, ordinal) <= (previous_page, previous_ordinal):
            raise ValueError('chunks must be ordered by strictly increasing (page_no, ordinal).')
        text = chunk.text
        if not isinstance(text, str) or not text or '\x00' in text:
            raise ValueError('chunk text must be a non-empty string without NUL.')
        if len(text) > MAX_CHUNK_CHARS:
            raise ValueError(f'chunk text exceeds {MAX_CHUNK_CHARS} characters.')
        total += len(text)
        if total > MAX_TEXT_CHARS:
            raise ValueError(f'chunk text total exceeds {MAX_TEXT_CHARS} characters.')
        rows.append((document_id, page_no, ordinal, text, chunk.chunk_type, chunk.heading_level))
        previous_page, previous_ordinal = page_no, ordinal
    return rows


def _is_active_sha_conflict(exc: psycopg.errors.UniqueViolation) -> bool:
    """Whether ``exc`` is exactly the live-document SHA unique index.

    Every other integrity failure is re-raised by the caller, so an unrelated
    constraint is never reported to the client as a duplicate file.
    """
    diag = getattr(exc, 'diag', None)
    return getattr(diag, 'constraint_name', None) == ACTIVE_SHA_CONSTRAINT


def chunk_search_query(
    user_id: int,
    query: str,
    *,
    knowledge_base_id: str | None = None,
    document_id: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> tuple[str, list[Any]]:
    """Chunk full-text query plus parameters.

    Every branch keeps the caller-ownership, liveness and readiness predicates,
    so a search can never widen beyond ``user_id``'s ready documents. ``limit``
    and ``offset`` are clamped server-side and stay bound parameters; the caller's
    query text is never spliced into the statement.
    """
    clauses = [
        'kb.user_id = %s',
        'kb.deleted_at IS NULL',
        'd.deleted_at IS NULL',
        "d.state = 'ready'",
        "c.search_vector @@ plainto_tsquery('jiebaqry', %s)",
    ]
    filter_params: list[Any] = []
    if knowledge_base_id is not None:
        clauses.append('d.knowledge_base_id = %s')
        filter_params.append(knowledge_base_id)
    if document_id is not None:
        clauses.append('d.id = %s')
        filter_params.append(document_id)
    sql = f"""
        SELECT c.document_id, d.knowledge_base_id, d.filename, d.version, c.page_no, c.ordinal,
               c.text, c.chunk_type, c.heading_level
        FROM document_chunk c
        JOIN document d ON d.id = c.document_id
        JOIN knowledge_base kb ON kb.id = d.knowledge_base_id
        WHERE {' AND '.join(clauses)}
        ORDER BY ts_rank(c.search_vector, plainto_tsquery('jiebaqry', %s)) DESC,
                 d.created_at DESC, d.id ASC, c.page_no ASC, c.ordinal ASC
        LIMIT %s OFFSET %s
    """
    return sql, [int(user_id), query, *filter_params, query, _bounded(limit), _offset(offset)]


class KnowledgeRepository:
    """Private knowledge base data access, bound to the storage connection.

    Ownership is only ``knowledge_base.user_id``: ``document`` carries no second
    owner column, so every read and write joins back to the base and filters the
    caller there. Unknown ids and other people's ids both come back as
    ``None``/``False``/``[]``, which the HTTP layer will map to 404 without
    revealing whether the row exists.

    Returned rows are internal storage rows and may contain ``document.s3_key``;
    that key is for the upload/cleanup paths and must never be rendered by an
    HTTP response.
    """

    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    # -- knowledge base -------------------------------------------------

    async def create_knowledge_base(self, user_id: int, name: str) -> dict[str, Any]:
        clean = _clean_text(name, field='Knowledge base name', max_length=MAX_NAME_LENGTH)
        now = utc_now_dt()
        kb_id = str(uuid4())
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO knowledge_base (id, user_id, name, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (kb_id, int(user_id), clean, now, now),
            )
        created = await self.get_knowledge_base(user_id, kb_id)
        if created is None:
            raise RuntimeError(f'Failed to create knowledge base {clean}.')
        return created

    async def list_knowledge_bases(
        self,
        user_id: int,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT * FROM knowledge_base
                WHERE user_id = %s AND deleted_at IS NULL
                ORDER BY created_at DESC, id ASC
                LIMIT %s OFFSET %s
                """,
                (int(user_id), _bounded(limit), _offset(offset)),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def get_knowledge_base(self, user_id: int, knowledge_base_id: Any) -> dict[str, Any] | None:
        kb_id = _as_uuid(knowledge_base_id)
        if kb_id is None:
            return None
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT * FROM knowledge_base
                WHERE id = %s AND user_id = %s AND deleted_at IS NULL
                """,
                (kb_id, int(user_id)),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def rename_knowledge_base(self, user_id: int, knowledge_base_id: Any, name: str) -> bool:
        """Rename one owned base. ``False`` when it is unknown or not the caller's."""
        kb_id = _as_uuid(knowledge_base_id)
        if kb_id is None:
            return False
        clean = _clean_text(name, field='Knowledge base name', max_length=MAX_NAME_LENGTH)
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE knowledge_base
                SET name = %s, updated_at = %s
                WHERE id = %s AND user_id = %s AND deleted_at IS NULL
                """,
                (clean, utc_now_dt(), kb_id, int(user_id)),
            )
            return cur.rowcount > 0

    async def delete_knowledge_base(self, user_id: int, knowledge_base_id: Any) -> bool:
        """Tombstone one owned base and its documents, dropping chunk text.

        The base row is tombstoned first so the parent is locked (``FOR NO KEY
        UPDATE``, taken implicitly by that statement) for the rest of the
        caller's transaction: a concurrent ``create_document`` either commits
        before this one and is swept by the statements below, or waits on the
        lock and then fails its parent predicate. If the base row cannot be
        tombstoned the owned/active predicate did not match, so nothing else runs
        and ``False`` comes back.

        The base now reads as deleted, so the document statements below filter on
        the base **owner** only; keeping the ``deleted_at IS NULL`` predicate
        there would filter out the rows just being swept. Chunks are derived
        retrieval content and go; document rows stay as tombstones and keep
        ``s3_key`` until a cleanup job removes the stored object. The blobs
        themselves are not deleted here.

        PostgreSQL locking (READ COMMITTED) is the point of this ordering; it has
        not been exercised against a real PostgreSQL server.
        """
        kb_id = _as_uuid(knowledge_base_id)
        if kb_id is None:
            return False
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE knowledge_base SET deleted_at = %s, updated_at = %s
                WHERE id = %s AND user_id = %s AND deleted_at IS NULL
                """,
                (now, now, kb_id, int(user_id)),
            )
            if cur.rowcount == 0:
                return False
            await cur.execute(
                """
                UPDATE document SET deleted_at = %s, state = 'deleted', updated_at = %s,
                                    locked_by = NULL, lease_token = NULL, locked_at = NULL
                WHERE knowledge_base_id = %s AND deleted_at IS NULL
                  AND knowledge_base_id IN (
                      SELECT id FROM knowledge_base WHERE id = %s AND user_id = %s
                  )
                """,
                (now, now, kb_id, kb_id, int(user_id)),
            )
            await cur.execute(
                """
                DELETE FROM document_chunk
                WHERE document_id IN (
                    SELECT d.id FROM document d
                    JOIN knowledge_base kb ON kb.id = d.knowledge_base_id
                    WHERE d.knowledge_base_id = %s AND kb.user_id = %s
                )
                """,
                (kb_id, int(user_id)),
            )
        return True

    # -- document metadata ----------------------------------------------

    async def create_document(
        self,
        user_id: int,
        knowledge_base_id: Any,
        *,
        filename: str,
        mime_type: str | None = None,
        size_bytes: int | None = None,
        sha256: str | None = None,
        s3_key: str | None = None,
        page_count: int | None = None,
    ) -> dict[str, Any] | None:
        """Register document metadata under an owned base.

        Returns ``None`` when the base is unknown or belongs to somebody else.
        The parent row is read in a ``MATERIALIZED`` CTE that locks it ``FOR
        SHARE`` while applying the owner/active predicate: a concurrent
        ``delete_knowledge_base`` tombstoning that base conflicts with this share
        lock, so this row cannot be committed after the delete's sweep has
        already passed it. The CTE is materialized so the planner cannot inline
        the lock away.

        ``FOR SHARE`` is deliberate instead of ``FOR KEY SHARE``: the delete's
        ``deleted_at`` update takes ``FOR NO KEY UPDATE``, which does not conflict
        with ``FOR KEY SHARE`` and would therefore not serialize. This is
        PostgreSQL lock behaviour and has not been exercised against a real
        PostgreSQL server.

        ``sha256``/``size_bytes`` are set by the upload path once the stream has
        been read; they are not a client claim. Two live documents in the same
        base with the same non-null ``sha256`` violate the unique index and raise,
        which is left to the import package to turn into a dedupe response.
        """
        kb_id = _as_uuid(knowledge_base_id)
        if kb_id is None:
            return None
        clean = _clean_text(filename, field='Filename', max_length=MAX_FILENAME_LENGTH)
        now = utc_now_dt()
        document_id = str(uuid4())
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                WITH parent AS MATERIALIZED (
                    SELECT kb.id AS knowledge_base_id
                    FROM knowledge_base kb
                    WHERE kb.id = %s AND kb.user_id = %s AND kb.deleted_at IS NULL
                    FOR SHARE
                )
                INSERT INTO document (id, knowledge_base_id, filename, mime_type, size_bytes,
                                      sha256, s3_key, state, page_count, created_at, updated_at)
                SELECT %s, parent.knowledge_base_id, %s, %s, %s, %s, %s, 'pending_upload',
                       %s, %s, %s
                FROM parent
                """,
                (
                    kb_id,
                    int(user_id),
                    document_id,
                    clean,
                    mime_type,
                    size_bytes,
                    sha256,
                    s3_key,
                    page_count,
                    now,
                    now,
                ),
            )
        return await self.get_document(user_id, document_id)

    async def list_documents(
        self,
        user_id: int,
        knowledge_base_id: Any,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        kb_id = _as_uuid(knowledge_base_id)
        if kb_id is None:
            return []
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT d.* FROM document d
                JOIN knowledge_base kb ON kb.id = d.knowledge_base_id
                WHERE d.knowledge_base_id = %s AND kb.user_id = %s
                  AND kb.deleted_at IS NULL AND d.deleted_at IS NULL
                ORDER BY d.created_at DESC, d.id ASC
                LIMIT %s OFFSET %s
                """,
                (kb_id, int(user_id), _bounded(limit), _offset(offset)),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def get_document(self, user_id: int, document_id: Any) -> dict[str, Any] | None:
        doc_id = _as_uuid(document_id)
        if doc_id is None:
            return None
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT d.* FROM document d
                JOIN knowledge_base kb ON kb.id = d.knowledge_base_id
                WHERE d.id = %s AND kb.user_id = %s
                  AND kb.deleted_at IS NULL AND d.deleted_at IS NULL
                """,
                (doc_id, int(user_id)),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_document(self, user_id: int, document_id: Any) -> bool:
        """Tombstone one owned document and drop its chunk text.

        The tombstone also clears the lease fields, so a worker holding an old
        lease cannot pick the document back up. The row survives with its
        ``s3_key`` so the stored object can be cleaned up later; this does not
        claim the blob is already gone. Returns ``False`` when the document is
        unknown or not the caller's, and stays ``False`` on a second call because
        the owned/active predicate no longer matches.
        """
        doc_id = _as_uuid(document_id)
        if doc_id is None:
            return False
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE document SET deleted_at = %s, state = 'deleted', updated_at = %s,
                                    locked_by = NULL, lease_token = NULL, locked_at = NULL
                WHERE id = %s AND deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM knowledge_base kb
                      WHERE kb.id = document.knowledge_base_id
                        AND kb.user_id = %s AND kb.deleted_at IS NULL
                  )
                """,
                (now, now, doc_id, int(user_id)),
            )
            deleted = cur.rowcount > 0
            if not deleted:
                return False
            await cur.execute(
                """
                DELETE FROM document_chunk
                WHERE document_id = %s
                  AND document_id IN (
                      SELECT d.id FROM document d
                      JOIN knowledge_base kb ON kb.id = d.knowledge_base_id
                      WHERE d.id = %s AND kb.user_id = %s
                  )
                """,
                (doc_id, doc_id, int(user_id)),
            )
        return True

    # -- upload state machine -------------------------------------------

    async def begin_upload(
        self,
        user_id: int,
        document_id: Any,
        upload_token: Any,
        *,
        s3_key: str,
    ) -> bool:
        """CAS ``pending_upload -> uploading`` for one owned, live document.

        The attempt token and the server-built object key are written together,
        before the object store is contacted, so a later cleanup can still find
        the orphan. ``False`` covers an unknown or malformed id, a foreign
        owner, a parent that is gone, or a row that is not a free
        ``pending_upload`` row. Nothing here touches HTTP, the object store,
        ``attempts`` or ``version``; the long upload runs after the caller
        commits this transaction.
        """
        doc_id = _as_uuid(document_id)
        token = _as_uuid(upload_token)
        if doc_id is None or token is None:
            return False
        key = _clean_upload_key(s3_key)
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE document
                SET state = 'uploading', lease_token = %s, locked_by = %s, locked_at = %s,
                    s3_key = %s, updated_at = %s
                WHERE id = %s AND state = 'pending_upload' AND deleted_at IS NULL
                  AND s3_key IS NULL AND lease_token IS NULL
                  AND EXISTS (
                      SELECT 1 FROM knowledge_base kb
                      WHERE kb.id = document.knowledge_base_id
                        AND kb.user_id = %s AND kb.deleted_at IS NULL
                  )
                """,
                (token, UPLOAD_LOCK_OWNER, now, key, now, doc_id, int(user_id)),
            )
            return cur.rowcount > 0

    async def finish_upload(
        self,
        user_id: int,
        document_id: Any,
        upload_token: Any,
        *,
        size_bytes: int,
        sha256: str,
    ) -> Literal['queued', 'stale', 'duplicate']:
        """Record the real size/hash of the current attempt and queue the row.

        Only the current ``uploading`` token of a live owned document moves to
        ``queued``; a tombstone, a foreign row, a superseded token or an unknown
        id all answer ``stale`` with no side effect. The journal key written by
        :meth:`begin_upload` is kept for cleanup.

        The statement runs inside a savepoint: a ``UniqueViolation`` on the live
        SHA index is the only failure turned into ``duplicate`` (the losing row
        is *not* queued), and any failure is rolled back to the savepoint so the
        caller's transaction stays usable. If that rollback/release itself fails
        the transaction was not restored, so the cleanup error is raised instead
        of a normal result, with the original failure kept as its cause for
        internal diagnostics. Other errors are re-raised unchanged. This method
        never commits or rolls back the caller's transaction.
        """
        doc_id = _as_uuid(document_id)
        token = _as_uuid(upload_token)
        if doc_id is None or token is None:
            return 'stale'
        size = _upload_size(size_bytes)
        digest = _upload_sha256(sha256)
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            await cur.execute(f'SAVEPOINT {FINISH_SAVEPOINT}')
            try:
                await cur.execute(
                    """
                    UPDATE document
                    SET state = 'queued', size_bytes = %s, sha256 = %s, updated_at = %s,
                        error_code = NULL, error_message = NULL,
                        locked_by = NULL, lease_token = NULL, locked_at = NULL
                    WHERE id = %s AND state = 'uploading' AND lease_token = %s
                      AND deleted_at IS NULL
                      AND EXISTS (
                          SELECT 1 FROM knowledge_base kb
                          WHERE kb.id = document.knowledge_base_id
                            AND kb.user_id = %s AND kb.deleted_at IS NULL
                      )
                    """,
                    (size, digest, now, doc_id, token, int(user_id)),
                )
                queued = cur.rowcount > 0
            except BaseException as exc:
                # Roll back to the savepoint for every failure (cancellation included)
                # so the caller's transaction stays usable. The cleanup itself must
                # succeed before any normal answer: a failed ROLLBACK TO or RELEASE
                # leaves the transaction unusable, so that error is propagated with
                # the original failure as its cause instead of reporting ``duplicate``.
                try:
                    await cur.execute(f'ROLLBACK TO SAVEPOINT {FINISH_SAVEPOINT}')
                    await cur.execute(f'RELEASE SAVEPOINT {FINISH_SAVEPOINT}')
                except BaseException as cleanup_error:
                    raise cleanup_error from exc
                if isinstance(exc, psycopg.errors.UniqueViolation) and _is_active_sha_conflict(exc):
                    return 'duplicate'
                raise
            await cur.execute(f'RELEASE SAVEPOINT {FINISH_SAVEPOINT}')
        return 'queued' if queued else 'stale'

    async def fail_upload(
        self,
        user_id: int,
        document_id: Any,
        upload_token: Any,
        *,
        error_code: str,
        error_message: str,
        cleanup_succeeded: bool = False,
    ) -> bool:
        """CAS the current attempt to ``failed``, keeping the journal key.

        ``cleanup_succeeded`` may only be ``True`` when a trusted storage
        operation confirmed this attempt's object is gone; then the key is
        cleared, otherwise it stays for the later cleanup pass. It must be a
        real bool: any other value is rejected before SQL runs, so a truthy
        string can never wipe the journalled key. The error pair is bounded
        ASCII written for developers, never client text. ``attempts``/``version``
        are deliberately left alone.
        """
        if not isinstance(cleanup_succeeded, bool):
            raise ValueError('cleanup_succeeded must be a boolean.')
        doc_id = _as_uuid(document_id)
        token = _as_uuid(upload_token)
        if doc_id is None or token is None:
            return False
        code = _clean_ascii(error_code, field='error_code', max_length=MAX_ERROR_CODE_LENGTH)
        message = _clean_ascii(error_message, field='error_message', max_length=MAX_ERROR_MESSAGE_LENGTH)
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE document
                SET state = 'failed', error_code = %s, error_message = %s, updated_at = %s,
                    s3_key = CASE WHEN %s THEN NULL ELSE s3_key END,
                    locked_by = NULL, lease_token = NULL, locked_at = NULL
                WHERE id = %s AND state = 'uploading' AND lease_token = %s
                  AND deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM knowledge_base kb
                      WHERE kb.id = document.knowledge_base_id
                        AND kb.user_id = %s AND kb.deleted_at IS NULL
                  )
                """,
                (code, message, now, cleanup_succeeded, doc_id, token, int(user_id)),
            )
            return cur.rowcount > 0

    async def retry_upload(
        self,
        user_id: int,
        document_id: Any,
        *,
        cleaned_key: str | None = None,
    ) -> bool:
        """Return an upload-failed document to ``pending_upload``.

        Only ``failed`` rows whose ``sha256`` is still empty qualify: a failure
        that already has a content hash came from parsing, not uploading, and
        every other state (including ``deleted``) is never revived. When the
        previous attempt journalled a key, ``cleaned_key`` must be that exact
        key - the internal result of a confirmed cleanup, never a client claim -
        or the retry is refused and the key survives. Errors and the lease are
        cleared; ``attempts``/``version`` are not touched.
        """
        doc_id = _as_uuid(document_id)
        if doc_id is None:
            return False
        if cleaned_key is not None and not isinstance(cleaned_key, str):
            raise ValueError('cleaned_key must be a string or None.')
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE document
                SET state = 'pending_upload', s3_key = NULL, updated_at = %s,
                    error_code = NULL, error_message = NULL,
                    locked_by = NULL, lease_token = NULL, locked_at = NULL
                WHERE id = %s AND state = 'failed' AND sha256 IS NULL
                  AND deleted_at IS NULL
                  AND (s3_key IS NULL OR s3_key = %s)
                  AND EXISTS (
                      SELECT 1 FROM knowledge_base kb
                      WHERE kb.id = document.knowledge_base_id
                        AND kb.user_id = %s AND kb.deleted_at IS NULL
                  )
                """,
                (now, doc_id, cleaned_key, int(user_id)),
            )
            return cur.rowcount > 0

    # -- import state machine -------------------------------------------

    async def list_queued_imports(self, *, limit: int = DEFAULT_PAGE_SIZE) -> list[dict[str, Any]]:
        """Worker queue candidates: fully uploaded, unclaimed, parseable rows.

        Internal only: the rows carry ``s3_key`` and ``kb.user_id`` and are never a
        response payload. Listing is not a claim, so a worker must still call
        :meth:`claim_import`; ordered ``created_at``/``id`` so polling is stable.
        """
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT d.*, kb.user_id
                FROM document d
                JOIN knowledge_base kb ON kb.id = d.knowledge_base_id
                WHERE d.state = 'queued' AND d.deleted_at IS NULL
                  AND kb.deleted_at IS NULL
                  AND d.locked_by IS NULL AND d.lease_token IS NULL
                  AND d.s3_key IS NOT NULL AND d.s3_key <> ''
                  AND d.sha256 IS NOT NULL AND d.sha256 <> ''
                  AND d.size_bytes IS NOT NULL AND d.size_bytes > 0
                  AND d.mime_type IN (%s, %s)
                ORDER BY d.created_at ASC, d.id ASC
                LIMIT %s
                """,
                (*IMPORT_MIME_TYPES, _bounded(limit)),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def claim_import(
        self,
        user_id: int,
        document_id: Any,
        worker_id: str,
        lease_token: Any,
    ) -> dict[str, Any] | None:
        """CAS one owned queued document to ``processing`` for ``worker_id``.

        ``worker_id`` is bounded ASCII and may not be the reserved ``upload``
        holder; ``lease_token`` is a canonical UUID. The CAS re-checks owner,
        liveness and the uploaded metadata, so a stale queue snapshot can never be
        claimed twice; ``attempts`` grows by one and a previous error is cleared.
        Returns the internal row on success, ``None`` on every miss with no side
        effect.
        """
        doc_id = _as_uuid(document_id)
        token = _as_uuid(lease_token)
        if doc_id is None or token is None:
            return None
        worker = _clean_ascii(worker_id, field='worker_id', max_length=MAX_WORKER_ID_LENGTH)
        if worker == UPLOAD_LOCK_OWNER:
            raise ValueError(f'worker_id cannot be the reserved {UPLOAD_LOCK_OWNER!r} holder.')
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE document
                SET state = 'processing', attempts = attempts + 1,
                    locked_by = %s, lease_token = %s, locked_at = %s,
                    error_code = NULL, error_message = NULL, updated_at = %s
                WHERE id = %s AND state = 'queued' AND deleted_at IS NULL
                  AND locked_by IS NULL AND lease_token IS NULL
                  AND s3_key IS NOT NULL AND s3_key <> ''
                  AND sha256 IS NOT NULL AND sha256 <> ''
                  AND size_bytes IS NOT NULL AND size_bytes > 0
                  AND mime_type IN (%s, %s)
                  AND EXISTS (
                      SELECT 1 FROM knowledge_base kb
                      WHERE kb.id = document.knowledge_base_id
                        AND kb.user_id = %s AND kb.deleted_at IS NULL
                  )
                """,
                (worker, token, now, now, doc_id, *IMPORT_MIME_TYPES, int(user_id)),
            )
            claimed = cur.rowcount > 0
        if not claimed:
            return None
        return await self.get_document(user_id, doc_id)

    async def heartbeat_import(self, user_id: int, document_id: Any, lease_token: Any) -> bool:
        """Refresh the lease timestamp of the current ``processing`` holder.

        The only column written is ``locked_at``: the row keeps its state, holder,
        token, business ``updated_at``, counters, uploaded source and chunks. Only
        the current lease token of a live owned document qualifies, and the
        reserved ``upload`` holder can never refresh an import lease. Every miss -
        unknown or malformed id, foreign owner, tombstoned document or parent,
        superseded token, other state - answers ``False`` with no side effect. The
        caller owns the short transaction; this method never commits or rolls
        back.

        Two fixed server-side limits are set on that transaction before the
        UPDATE - ``SET LOCAL lock_timeout = '5s'`` and ``SET LOCAL
        statement_timeout = '10s'`` - so a heartbeat cannot wait out a long lock
        holder. Because they are ``SET LOCAL``, the caller's commit or rollback
        undoes them and the pooled connection is left unchanged for its next
        user.
        """
        doc_id = _as_uuid(document_id)
        token = _as_uuid(lease_token)
        if doc_id is None or token is None:
            return False
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            # Bound the lock wait and the statement duration for this write only.
            await cur.execute("SET LOCAL lock_timeout = '5s'")
            await cur.execute("SET LOCAL statement_timeout = '10s'")
            await cur.execute(
                """
                UPDATE document
                SET locked_at = %s
                WHERE id = %s AND state = 'processing' AND lease_token = %s
                  AND deleted_at IS NULL
                  AND locked_by IS NOT NULL AND locked_by <> %s
                  AND EXISTS (
                      SELECT 1 FROM knowledge_base kb
                      WHERE kb.id = document.knowledge_base_id
                        AND kb.user_id = %s AND kb.deleted_at IS NULL
                  )
                """,
                (now, doc_id, token, UPLOAD_LOCK_OWNER, int(user_id)),
            )
            return cur.rowcount > 0

    async def complete_import(
        self,
        user_id: int,
        document_id: Any,
        lease_token: Any,
        *,
        page_count: int,
        chunks: Sequence[Chunk],
    ) -> bool:
        """CAS the current lease to ``ready`` and store the whole parse result.

        The parse result is validated before any SQL runs. Only the current
        ``processing`` token of a live owned document can publish; the successful
        CAS bumps ``version``, then the derived chunks are replaced and inserted
        in batches of at most :data:`IMPORT_CHUNK_BATCH_SIZE`. A failed CAS changes
        no chunk. This method never commits or rolls back the caller's
        transaction: any insert failure propagates so the outer transaction can
        roll the whole publish back.
        """
        doc_id = _as_uuid(document_id)
        token = _as_uuid(lease_token)
        if doc_id is None or token is None:
            return False
        pages = _import_page_count(page_count)
        rows = _import_chunk_rows(doc_id, pages, chunks)
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE document
                SET state = 'ready', page_count = %s, version = version + 1, updated_at = %s,
                    error_code = NULL, error_message = NULL,
                    locked_by = NULL, lease_token = NULL, locked_at = NULL
                WHERE id = %s AND state = 'processing' AND lease_token = %s
                  AND deleted_at IS NULL
                  AND locked_by IS NOT NULL AND locked_by <> %s
                  AND EXISTS (
                      SELECT 1 FROM knowledge_base kb
                      WHERE kb.id = document.knowledge_base_id
                        AND kb.user_id = %s AND kb.deleted_at IS NULL
                  )
                """,
                (pages, now, doc_id, token, UPLOAD_LOCK_OWNER, int(user_id)),
            )
            if cur.rowcount == 0:
                return False
            await cur.execute('DELETE FROM document_chunk WHERE document_id = %s', (doc_id,))
            for start in range(0, len(rows), IMPORT_CHUNK_BATCH_SIZE):
                await cur.executemany(
                    """
                    INSERT INTO document_chunk
                        (document_id, page_no, ordinal, text, chunk_type, heading_level)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    rows[start : start + IMPORT_CHUNK_BATCH_SIZE],
                )
        return True

    async def fail_import(
        self,
        user_id: int,
        document_id: Any,
        lease_token: Any,
        *,
        error_code: str,
        error_message: str,
    ) -> bool:
        """CAS the current lease to ``failed``, keeping the uploaded source.

        Only the current ``processing`` token of a live owned document moves; the
        source key, hash and size stay so a later retry can resume, while
        ``locked_by``/error text and the lease are written. The error pair is
        bounded ASCII for developers; ``attempts``/``version`` are not touched.
        """
        doc_id = _as_uuid(document_id)
        token = _as_uuid(lease_token)
        if doc_id is None or token is None:
            return False
        code = _clean_ascii(error_code, field='error_code', max_length=MAX_ERROR_CODE_LENGTH)
        message = _clean_ascii(error_message, field='error_message', max_length=MAX_ERROR_MESSAGE_LENGTH)
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE document
                SET state = 'failed', error_code = %s, error_message = %s, updated_at = %s,
                    locked_by = NULL, lease_token = NULL, locked_at = NULL
                WHERE id = %s AND state = 'processing' AND lease_token = %s
                  AND deleted_at IS NULL
                  AND locked_by IS NOT NULL AND locked_by <> %s
                  AND EXISTS (
                      SELECT 1 FROM knowledge_base kb
                      WHERE kb.id = document.knowledge_base_id
                        AND kb.user_id = %s AND kb.deleted_at IS NULL
                  )
                """,
                (code, message, now, doc_id, token, UPLOAD_LOCK_OWNER, int(user_id)),
            )
            return cur.rowcount > 0

    async def retry_import(self, user_id: int, document_id: Any) -> bool:
        """Return one owned parse failure to ``queued``, source unchanged.

        Only ``failed`` rows with a complete uploaded source (key, hash, positive
        size and a supported MIME) and no lease qualify: upload failures are
        handled by :meth:`retry_upload`, and no other state is ever revived. Error
        text and the lease are cleared; ``attempts``/``version`` are not touched.
        """
        doc_id = _as_uuid(document_id)
        if doc_id is None:
            return False
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE document
                SET state = 'queued', error_code = NULL, error_message = NULL, updated_at = %s
                WHERE id = %s AND state = 'failed' AND deleted_at IS NULL
                  AND locked_by IS NULL AND lease_token IS NULL
                  AND s3_key IS NOT NULL AND s3_key <> ''
                  AND sha256 IS NOT NULL AND sha256 <> ''
                  AND size_bytes IS NOT NULL AND size_bytes > 0
                  AND mime_type IN (%s, %s)
                  AND EXISTS (
                      SELECT 1 FROM knowledge_base kb
                      WHERE kb.id = document.knowledge_base_id
                        AND kb.user_id = %s AND kb.deleted_at IS NULL
                  )
                """,
                (now, doc_id, *IMPORT_MIME_TYPES, int(user_id)),
            )
            return cur.rowcount > 0

    async def requeue_expired_imports(self, *, locked_before: datetime, limit: int = DEFAULT_PAGE_SIZE) -> int:
        """Return expired ``processing`` leases to ``queued``. Internal worker only.

        ``locked_before`` must be a timezone-aware datetime and is normalized to
        UTC; the comparison is strict, so a lease expiring exactly at the cutoff is
        kept. A bounded candidate set is read by ``(locked_at, id)`` and the outer
        UPDATE repeats the state, expiry, holder, token and liveness predicates, so
        a lease renewed by a heartbeat between that snapshot and the row lock is
        skipped instead of reclaimed from a stale snapshot. Rows without a holder,
        with the reserved ``upload`` holder, without a token or without
        ``locked_at`` are never candidates, and a reclaim clears the lease and the
        error text while source, chunks, ``attempts`` and ``version`` stay.

        This is global maintenance (no owner parameter, never an HTTP endpoint) and
        it never touches the object store; a stored object still being written by
        another path is not this method's concern. Returns the real number of
        reclaimed rows. The caller owns the transaction.

        The two fixed server-side limits - ``SET LOCAL lock_timeout = '5s'`` and
        ``SET LOCAL statement_timeout = '10s'`` - are set on the caller's
        transaction before the maintenance UPDATE, so a stale-lease reclaim
        cannot wait out a long lock holder. ``SET LOCAL`` is undone by the
        caller's commit or rollback, leaving no residue on the pooled
        connection.

        shortcut: PostgreSQL READ COMMITTED re-evaluation after waiting on a
        concurrently updated row is not exercised here; the SQLite harness proves
        the SQL logic only, not real concurrency.
        """
        if not isinstance(locked_before, datetime) or locked_before.tzinfo is None:
            raise ValueError('locked_before must be a timezone-aware datetime.')
        cutoff = to_utc_dt(locked_before)
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            # Bound the lock wait and the statement duration for this write only.
            await cur.execute("SET LOCAL lock_timeout = '5s'")
            await cur.execute("SET LOCAL statement_timeout = '10s'")
            await cur.execute(
                """
                WITH expired AS (
                    SELECT d.id AS id
                    FROM document d
                    JOIN knowledge_base kb ON kb.id = d.knowledge_base_id
                    WHERE d.state = 'processing' AND d.deleted_at IS NULL
                      AND kb.deleted_at IS NULL
                      AND d.locked_by IS NOT NULL AND d.locked_by <> %s
                      AND d.lease_token IS NOT NULL
                      AND d.locked_at IS NOT NULL AND d.locked_at < %s
                    ORDER BY d.locked_at ASC, d.id ASC
                    LIMIT %s
                )
                UPDATE document
                SET state = 'queued', updated_at = %s, locked_by = NULL,
                    lease_token = NULL, locked_at = NULL,
                    error_code = NULL, error_message = NULL
                WHERE id IN (SELECT id FROM expired)
                  AND state = 'processing' AND deleted_at IS NULL
                  AND locked_by IS NOT NULL AND locked_by <> %s
                  AND lease_token IS NOT NULL
                  AND locked_at IS NOT NULL AND locked_at < %s
                  AND EXISTS (
                      SELECT 1 FROM knowledge_base kb
                      WHERE kb.id = document.knowledge_base_id
                        AND kb.deleted_at IS NULL
                  )
                """,
                (UPLOAD_LOCK_OWNER, cutoff, _bounded(limit), now, UPLOAD_LOCK_OWNER, cutoff),
            )
            return cur.rowcount

    # -- chunks ----------------------------------------------------------

    async def list_chunks(
        self,
        user_id: int,
        document_id: Any,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Chunks of one owned, live document in reading order."""
        doc_id = _as_uuid(document_id)
        if doc_id is None:
            return []
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT c.document_id, c.page_no, c.ordinal, c.text, c.chunk_type, c.heading_level
                FROM document_chunk c
                JOIN document d ON d.id = c.document_id
                JOIN knowledge_base kb ON kb.id = d.knowledge_base_id
                WHERE c.document_id = %s AND kb.user_id = %s
                  AND kb.deleted_at IS NULL AND d.deleted_at IS NULL
                ORDER BY c.page_no ASC, c.ordinal ASC
                LIMIT %s OFFSET %s
                """,
                (doc_id, int(user_id), _bounded(limit), _offset(offset)),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def search_chunks(
        self,
        user_id: int,
        query: str,
        *,
        knowledge_base_id: Any = None,
        document_id: Any = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Full-text search over the caller's live, ready chunks.

        PostgreSQL only: it needs the ``jiebaqry`` dictionary and the generated
        ``search_vector``. The whole chunk text is indexed, unlike the article
        search that truncates at 50000 characters. ``offset`` is a bound
        parameter, never spliced into the caller's query text.
        """
        clean = _clean_text(query, field='Search query', max_length=MAX_QUERY_LENGTH)
        kb_filter: str | None = None
        if knowledge_base_id is not None:
            kb_filter = _as_uuid(knowledge_base_id)
            if kb_filter is None:
                return []
        doc_filter: str | None = None
        if document_id is not None:
            doc_filter = _as_uuid(document_id)
            if doc_filter is None:
                return []
        sql, params = chunk_search_query(
            user_id,
            clean,
            knowledge_base_id=kb_filter,
            document_id=doc_filter,
            limit=limit,
            offset=offset,
        )
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def read_document_content(
        self,
        user_id: int,
        document_id: Any,
        *,
        page_no: int | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> dict[str, Any] | None:
        """One owned live document plus a bounded slice of its published chunks.

        ``None`` covers an unknown, malformed, foreign or tombstoned document. A
        document that is not ``ready`` comes back with an empty ``chunks`` list,
        so the caller decides whether that is a 409 instead of this reader
        guessing. The returned mapping holds the internal document row (which
        may carry ``s3_key``) and the public chunk columns only.

        The document is read first with ``FOR SHARE``, from a statement whose
        only ``FROM`` is ``document``: ownership is an ``EXISTS`` on
        ``knowledge_base``, so the lock lands on the document alone and never
        follows the parent-before-child lock order a base delete uses. That lock
        is held until the caller's short transaction ends, which stops a
        concurrent ``complete_import`` from bumping ``version`` and replacing the
        chunks between the two statements here (READ COMMITTED would otherwise
        let each statement see a different commit). No network I/O happens while
        the lock is held and this method never commits or rolls back.

        At most ``limit + 1`` chunk rows are read so the caller can tell a page
        that is exactly full from one with a further row; the slice itself is
        returned untouched. Ownership is already established and held by the
        document lock above, so the chunk statement is keyed on ``document_id``
        alone - it does not join the base a second time. ``page_no``, when given,
        scopes the offset to that page. PostgreSQL row locks and the READ
        COMMITTED snapshot this pairing relies on are not exercised by the
        SQLite harness.
        """
        if page_no is not None and (isinstance(page_no, bool) or not isinstance(page_no, int) or page_no < 1):
            raise ValueError('page_no must be a positive integer or None.')
        doc_id = _as_uuid(document_id)
        if doc_id is None:
            return None
        size = _bounded(limit)
        start = _offset(offset)
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT d.* FROM document d
                WHERE d.id = %s AND d.deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM knowledge_base kb
                      WHERE kb.id = d.knowledge_base_id
                        AND kb.user_id = %s AND kb.deleted_at IS NULL
                  )
                FOR SHARE
                """,
                (doc_id, int(user_id)),
            )
            document = await cur.fetchone()
            if document is None:
                return None
            doc = dict(document)
            if doc.get('state') != 'ready':
                return {'document': doc, 'chunks': []}
            clauses = ['c.document_id = %s']
            params: list[Any] = [doc_id]
            if page_no is not None:
                clauses.append('c.page_no = %s')
                params.append(page_no)
            await cur.execute(
                f"""
                SELECT c.page_no, c.ordinal, c.text, c.chunk_type, c.heading_level
                FROM document_chunk c
                WHERE {' AND '.join(clauses)}
                ORDER BY c.page_no ASC, c.ordinal ASC
                LIMIT %s OFFSET %s
                """,
                (*params, size + 1, start),
            )
            rows = await cur.fetchall()
        return {'document': doc, 'chunks': [dict(row) for row in rows]}
