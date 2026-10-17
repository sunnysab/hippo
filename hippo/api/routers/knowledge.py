"""Owner-scoped metadata and raw-upload HTTP endpoints for private knowledge bases.

The metadata routes create, list, rename and tombstone knowledge bases, and
register, read and tombstone document records. ``PUT /document/{id}/file`` is a
thin adapter over ``receive_document_file``: it owns the HTTP gate (request body,
headers, concurrency) while the upload service owns the state machine, the
private file and the object store. ``POST /document/{id}/retry`` is the same
shape over ``retry_document``: owner/state precheck, the shared concurrency gate
and the public projection, with no client input deciding what is retried.
``GET /knowledge-base/{id}/search`` is a read-only full-text search over the
base's ready chunks; it returns a fixed hit projection and no raw storage row.
Nothing here reaches S3, invents an upload URL, or serializes an internal
document row.

``current_user`` is the only source of ownership, and every repository call is
scoped to that user id, so a malformed id, an unknown id, another user's row and
a tombstoned row all answer the same 404.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request, Response

from ...exceptions import ApiError
from ...knowledge.retry import retry_document
from ...knowledge.upload import (
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
    receive_document_file,
)
from ...models import User
from ...repositories.knowledge import DEFAULT_PAGE_SIZE, MAX_OFFSET, MAX_PAGE_SIZE, MAX_QUERY_LENGTH
from ...storage import PostgresStorage
from ..deps import current_user, get_storage

router = APIRouter(tags=['knowledge'])

#: Response fields a caller may see. Everything else on the row - the object key,
#: the content hash, the lease pair and the owner id - stays server-side.
_KNOWLEDGE_BASE_FIELDS = ('id', 'name', 'created_at', 'updated_at')
_DOCUMENT_FIELDS = (
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

#: Request fields a caller may set. Everything else is server-derived.
_KNOWLEDGE_BASE_INPUT = frozenset({'name'})
_DOCUMENT_INPUT = frozenset({'filename', 'mime_type'})

#: Accepted MIME types, in their canonical spelling.
_ALLOWED_MIME_TYPES = frozenset({'application/pdf', 'text/plain'})

#: Chunk fields the content endpoint returns: the derived text and where it sits,
#: never the internal ``document_id``.
_CHUNK_FIELDS = ('page_no', 'ordinal', 'text', 'chunk_type', 'heading_level')

#: One content page: reading a document, not exporting it. The default and the
#: cap are deliberately the same value.
CONTENT_PAGE_SIZE = 100
MAX_CONTENT_PAGE_SIZE = 100
#: Pages a ``page_no`` filter may address; the document's own ``page_count`` is
#: checked against the row, this is only the parameter bound.
MAX_CONTENT_PAGE_NO = 1000

#: One search page and the cap a caller may ask for.
SEARCH_PAGE_SIZE = 20
MAX_SEARCH_PAGE_SIZE = 100

#: The public search hit: where the text comes from and the version it belongs
#: to. Never the object key, digest, lease or owner id of the storage row.
_SEARCH_HIT_FIELDS = (
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


def _public_search_hit(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in _SEARCH_HIT_FIELDS}


def _public_chunk(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in _CHUNK_FIELDS}


def _public_knowledge_base(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in _KNOWLEDGE_BASE_FIELDS}


def _public_document(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in _DOCUMENT_FIELDS}


def _reject_unknown_fields(body: dict[str, Any], allowed: frozenset[str], *, code: str) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ApiError(f'Unknown field: {unknown[0]}', status=400, code=code)


def _invalid_input(exc: ValueError) -> ApiError:
    """The repository's own bounds check, restated as a 400.

    Those messages are ASCII and never carry SQL or the offending value.
    """
    return ApiError(str(exc), status=400, code='knowledge.invalid_input')


def _knowledge_base_not_found() -> ApiError:
    return ApiError('Knowledge base not found.', status=404, code='knowledge_base.not_found')


def _document_not_found() -> ApiError:
    return ApiError('Document not found.', status=404, code='document.not_found')


def _document_page_not_found() -> ApiError:
    return ApiError('Document page not found.', status=404, code='document.page_not_found')


def _canonical_mime(body: dict[str, Any]) -> str:
    """The one accepted MIME value, or a 400/415.

    Only the canonical spellings are stored, so no parameter, case or whitespace
    variant ever reaches the row. Missing or non-string input is a bad request;
    anything else is a media type this service does not keep.
    """
    if 'mime_type' not in body:
        raise ApiError('mime_type is required.', status=400, code='document.mime_type_required')
    value = body['mime_type']
    if not isinstance(value, str):
        raise ApiError('mime_type must be a string.', status=400, code='document.invalid_mime')
    if value not in _ALLOWED_MIME_TYPES:
        raise ApiError(
            'Unsupported MIME type; expected application/pdf or text/plain.',
            status=415,
            code='document.unsupported_mime',
        )
    return value


@router.get('/knowledge-base')
async def list_knowledge_bases(
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    rows = await storage.knowledge.list_knowledge_bases(user.id, limit=limit, offset=offset)
    return {'knowledge_bases': [_public_knowledge_base(row) for row in rows], 'limit': limit, 'offset': offset}


@router.post('/knowledge-base', status_code=201)
async def create_knowledge_base(
    body: dict[str, Any] = Body(...),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    _reject_unknown_fields(body, _KNOWLEDGE_BASE_INPUT, code='knowledge.unknown_field')
    try:
        async with storage.transaction():
            created = await storage.knowledge.create_knowledge_base(user.id, body.get('name'))
    except ValueError as exc:
        raise _invalid_input(exc) from exc
    return {'knowledge_base': _public_knowledge_base(created)}


@router.get('/knowledge-base/{knowledge_base_id}')
async def get_knowledge_base(
    knowledge_base_id: str,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    row = await storage.knowledge.get_knowledge_base(user.id, knowledge_base_id)
    if row is None:
        raise _knowledge_base_not_found()
    return {'knowledge_base': _public_knowledge_base(row)}


@router.patch('/knowledge-base/{knowledge_base_id}')
async def rename_knowledge_base(
    knowledge_base_id: str,
    body: dict[str, Any] = Body(...),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    _reject_unknown_fields(body, _KNOWLEDGE_BASE_INPUT, code='knowledge.unknown_field')
    try:
        async with storage.transaction():
            renamed = await storage.knowledge.rename_knowledge_base(user.id, knowledge_base_id, body.get('name'))
            if not renamed:
                raise _knowledge_base_not_found()
            updated = await storage.knowledge.get_knowledge_base(user.id, knowledge_base_id)
            if updated is None:
                raise _knowledge_base_not_found()
    except ValueError as exc:
        raise _invalid_input(exc) from exc
    return {'knowledge_base': _public_knowledge_base(updated)}


@router.delete('/knowledge-base/{knowledge_base_id}', status_code=204)
async def delete_knowledge_base(
    knowledge_base_id: str,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> Response:
    async with storage.transaction():
        deleted = await storage.knowledge.delete_knowledge_base(user.id, knowledge_base_id)
        if not deleted:
            raise _knowledge_base_not_found()
    return Response(status_code=204)


@router.get('/knowledge-base/{knowledge_base_id}/document')
async def list_documents(
    knowledge_base_id: str,
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    # The parent is checked first: an unknown, foreign or deleted base is a 404,
    # never an empty page that would claim the base exists but holds nothing.
    parent = await storage.knowledge.get_knowledge_base(user.id, knowledge_base_id)
    if parent is None:
        raise _knowledge_base_not_found()
    rows = await storage.knowledge.list_documents(user.id, knowledge_base_id, limit=limit, offset=offset)
    return {'documents': [_public_document(row) for row in rows], 'limit': limit, 'offset': offset}


@router.post('/knowledge-base/{knowledge_base_id}/document', status_code=201)
async def create_document(
    knowledge_base_id: str,
    body: dict[str, Any] = Body(...),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    _reject_unknown_fields(body, _DOCUMENT_INPUT, code='document.unknown_field')
    mime_type = _canonical_mime(body)
    try:
        async with storage.transaction():
            created = await storage.knowledge.create_document(
                user.id,
                knowledge_base_id,
                filename=body.get('filename'),
                mime_type=mime_type,
            )
            if created is None:
                raise _knowledge_base_not_found()
    except ValueError as exc:
        raise _invalid_input(exc) from exc
    return {'document': _public_document(created)}


@router.get('/document/{document_id}')
async def get_document(
    document_id: str,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    row = await storage.knowledge.get_document(user.id, document_id)
    if row is None:
        raise _document_not_found()
    return {'document': _public_document(row)}


@router.delete('/document/{document_id}', status_code=204)
async def delete_document(
    document_id: str,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> Response:
    async with storage.transaction():
        deleted = await storage.knowledge.delete_document(user.id, document_id)
        if not deleted:
            raise _document_not_found()
    return Response(status_code=204)


@router.get('/document/{document_id}/content')
async def get_document_content(
    document_id: str,
    limit: int = Query(CONTENT_PAGE_SIZE, ge=1, le=MAX_CONTENT_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
    page_no: int | None = Query(None, ge=1, le=MAX_CONTENT_PAGE_NO),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Read one bounded slice of an owned, ready document's derived text.

    The document is read and locked in one short transaction together with the
    chunks, so the returned ``version`` and the text belong to the same publish;
    nothing here reaches the object store or re-parses the file. A document that
    is not ``ready`` yet answers 409 instead of an empty page that would read as
    "parsed, no text". ``next_offset`` is only set when a further chunk row really
    exists, so a page that is exactly full is not claimed to have a successor.
    """
    async with storage.transaction():
        result = await storage.knowledge.read_document_content(
            user.id, document_id, page_no=page_no, limit=limit, offset=offset
        )
    if result is None:
        raise _document_not_found()
    document = result['document']
    if document.get('state') != 'ready':
        raise ApiError('The document is not ready.', status=409, code='document.not_ready')
    page_count = document.get('page_count')
    if page_no is not None and page_count is not None and page_no > page_count:
        raise _document_page_not_found()
    rows = result['chunks']
    has_more = len(rows) > limit
    chunks = [_public_chunk(row) for row in rows[:limit]]
    return {
        'document': _public_document(document),
        'chunks': chunks,
        'next_offset': offset + len(chunks) if has_more else None,
    }


@router.get('/knowledge-base/{knowledge_base_id}/search')
async def search_knowledge_base(
    knowledge_base_id: str,
    q: str = Query(..., min_length=1, max_length=MAX_QUERY_LENGTH),
    limit: int = Query(SEARCH_PAGE_SIZE, ge=1, le=MAX_SEARCH_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
    document_id: str | None = Query(None),
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Full-text search over one owned base's ready chunks.

    The base is resolved first, so an unknown, foreign or tombstoned base is a
    404 instead of an empty result that would claim it exists. An explicit
    ``document_id`` must be the caller's own live document inside *that* base and
    already ``ready``; the comparison uses the canonical ids read back from the
    database, never the caller's own text spelling.

    One short transaction covers the checks and the search, so the ``version``
    and the chunk text of a hit come from the same publish. At most ``limit + 1``
    rows are read to tell a page that is exactly full from one with a further
    row; ``next_offset`` is only set when that probe row really exists. The text
    is returned verbatim - plain UTF-8, including any HTML - and is never passed
    through ``ts_headline`` or HTML escaping.
    """
    clean = q.strip()
    if not clean:
        raise ApiError('The search query cannot be blank.', status=400, code='knowledge.invalid_query')
    async with storage.transaction():
        base = await storage.knowledge.get_knowledge_base(user.id, knowledge_base_id)
        if base is None:
            raise _knowledge_base_not_found()
        doc_filter: str | None = None
        if document_id is not None:
            document = await storage.knowledge.get_document(user.id, document_id)
            if document is None or str(document.get('knowledge_base_id')) != str(base['id']):
                raise _document_not_found()
            if document.get('state') != 'ready':
                raise ApiError('The document is not ready.', status=409, code='document.not_ready')
            doc_filter = str(document['id'])
        rows = await storage.knowledge.search_chunks(
            user.id,
            clean,
            knowledge_base_id=base['id'],
            document_id=doc_filter,
            limit=limit + 1,
            offset=offset,
        )
    has_more = len(rows) > limit
    hits = [_public_search_hit(row) for row in rows[:limit]]
    return {'hits': hits, 'next_offset': offset + len(hits) if has_more else None}


# -- raw document upload -----------------------------------------------------

#: Concurrent raw uploads one app holds before answering 429. A small fixed
#: number keeps one stalled client from occupying every request.
MAX_ACTIVE_UPLOADS = 2

#: A body declared as this type is read as the raw bytes of the document's own
#: canonical MIME type.
_GENERIC_UPLOAD_MIME = 'application/octet-stream'

#: Service failure code -> HTTP status. A code that is not listed is an internal
#: fault and answers a fixed 500 instead of echoing unknown text.
_UPLOAD_ERROR_STATUS = {
    CODE_NOT_FOUND: 404,
    CODE_CONFLICT: 409,
    CODE_DUPLICATE: 409,
    CODE_TOO_LARGE: 413,
    CODE_EMPTY: 400,
    CODE_UPLOAD_TIMEOUT: 408,
    CODE_UPLOAD_FAILED: 502,
    CODE_PERSISTENCE_FAILED: 503,
}


def _upload_semaphore(request: Request) -> asyncio.Semaphore:
    """The app's own upload gate, created on first use.

    Kept on ``app.state`` rather than at module level: a semaphore binds to the
    loop that first awaits it, and one process may serve several apps/loops.
    The raw upload and the failed-document retry share it, so a retry's object
    delete competes for the same bounded slots as an upload body.
    """
    state = request.app.state
    semaphore = getattr(state, 'knowledge_upload_semaphore', None)
    if semaphore is None:
        semaphore = asyncio.Semaphore(MAX_ACTIVE_UPLOADS)
        state.knowledge_upload_semaphore = semaphore
    return semaphore


def _check_content_length(raw: str | None) -> None:
    """Reject a declared length that cannot be a real body size.

    A missing length is fine: the body is chunked and the service counts the
    bytes it actually receives. A declared length is never trusted as the size.
    """
    if raw is None:
        return
    if not raw.isascii() or not raw.isdigit():
        raise ApiError('Invalid Content-Length.', status=400, code='document.invalid_content_length')
    # Leading zeros are still a decimal; the digit-length guard keeps ``int`` off
    # a huge string without changing the value.
    digits = raw.lstrip('0') or '0'
    if len(digits) > len(str(MAX_FILE_BYTES)):
        raise ApiError('The document exceeds the supported size limits.', status=413, code=CODE_TOO_LARGE)
    declared = int(digits)
    if declared == 0:
        raise ApiError('The document is empty.', status=400, code=CODE_EMPTY)
    if declared > MAX_FILE_BYTES:
        raise ApiError('The document exceeds the supported size limits.', status=413, code=CODE_TOO_LARGE)


def _declared_mime(request: Request) -> str | None:
    """The Content-Type media type, parameters and case stripped; ``None`` if absent."""
    raw = request.headers.get('content-type')
    if raw is None:
        return None
    return raw.split(';', 1)[0].strip().lower() or None


def _check_upload_headers(request: Request, document_mime: Any) -> None:
    """Reject encoded bodies and media types that are not raw document bytes.

    Only the canonical types the document row already holds, the generic binary
    type and a missing header are accepted; multipart, JSON, XML and every other
    declared type are refused before a body byte is read.
    """
    encoding = request.headers.get('content-encoding')
    if encoding is not None and encoding.strip().lower() not in ('', 'identity'):
        raise ApiError(
            'Compressed upload bodies are not supported.',
            status=415,
            code='document.unsupported_encoding',
        )
    declared = _declared_mime(request)
    if declared in (None, _GENERIC_UPLOAD_MIME, document_mime):
        return
    raise ApiError(
        'Unsupported Content-Type; send the raw document bytes.',
        status=415,
        code='document.unsupported_mime',
    )


async def _failed_document(storage: PostgresStorage, user_id: int, document_id: str) -> dict[str, Any]:
    """The caller's live, ``failed`` row, checked before the retry gate.

    A short transaction of its own, so the read never holds the connection while
    the object store is being waited on. A missing, foreign or tombstoned row is
    a 404 and every other state is a 409.
    """
    async with storage.transaction():
        row = await storage.knowledge.get_document(user_id, document_id)
    if row is None:
        raise _document_not_found()
    if row.get('state') != 'failed':
        raise ApiError('The document cannot be retried in its current state.', status=409, code=CODE_CONFLICT)
    return row


async def _pending_document(storage: PostgresStorage, user_id: int, document_id: str) -> dict[str, Any]:
    """The caller's live, ``pending_upload`` row, checked before any body byte.

    A short transaction of its own, so the read never holds the connection while
    the body or the object store is being waited on. A missing, foreign or
    tombstoned row is a 404 and every other state is a 409.
    """
    async with storage.transaction():
        row = await storage.knowledge.get_document(user_id, document_id)
    if row is None:
        raise _document_not_found()
    if row.get('state') != 'pending_upload':
        raise ApiError('The document is not waiting for an upload.', status=409, code=CODE_CONFLICT)
    return row


def _upload_failure(error: DocumentUploadError) -> ApiError:
    """Map one service failure code onto its contract status, or a fixed 500."""
    status = _UPLOAD_ERROR_STATUS.get(error.code)
    if status is None:
        return ApiError('Internal server error', status=500, code='internal_error')
    return ApiError(error.message, status=status, code=error.code)


@router.post('/document/{document_id}/retry')
async def retry_failed_document(
    document_id: str,
    request: Request,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Prepare the retry of one owned, live, ``failed`` document.

    The adapter owns the HTTP gate only - the owner/state precheck and the
    shared per-app concurrency gate - and takes no business input: no body, key,
    digest or cleanup flag from the client decides what happens. A 200 means the
    retry was prepared (back to ``pending_upload`` with the client expected to
    send the file again, or back to ``queued`` for the import worker), never that
    anything was re-uploaded. The response is the public projection.
    """
    await _failed_document(storage, user.id, document_id)
    semaphore = _upload_semaphore(request)
    if semaphore.locked():
        raise ApiError('Another upload is already in progress.', status=429, code='document.upload_busy')
    async with semaphore:
        try:
            row = await retry_document(storage=storage, user_id=user.id, document_id=document_id)
        except DocumentUploadError as error:
            raise _upload_failure(error) from error
    return {'document': _public_document(row)}


@router.put('/document/{document_id}/file', status_code=202)
async def upload_document_file(
    document_id: str,
    request: Request,
    storage: PostgresStorage = Depends(get_storage),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Receive one raw document body into the private object store.

    The adapter owns the HTTP gate only: the owner/state precheck, the size,
    type and encoding headers, and the per-app concurrency gate. The upload
    service owns the state machine, the private file and the object store; it is
    called with nothing but the request stream, so no client token, key, digest
    or file name can reach storage. The response is the public document
    projection, never the internal row.
    """
    document = await _pending_document(storage, user.id, document_id)
    _check_content_length(request.headers.get('content-length'))
    _check_upload_headers(request, document.get('mime_type'))
    semaphore = _upload_semaphore(request)
    if semaphore.locked():
        raise ApiError('Another upload is already in progress.', status=429, code='document.upload_busy')
    async with semaphore:
        try:
            row = await receive_document_file(
                storage=storage,
                user_id=user.id,
                document_id=document_id,
                chunks=request.stream(),
            )
        except DocumentUploadError as error:
            raise _upload_failure(error) from error
    return {'document': _public_document(row)}
