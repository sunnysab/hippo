"""Private knowledge-base, document and search schemas.

The routes project every response through a fixed field list, so these models
are closed on purpose: a field that is not here is a field this API never
promises (the object key, the content hash, the lease pair and the owner id all
stay server-side). Ids are declared as ``UUID`` because that is the column type
the reads return, never the driver's text form; the model serialises them back
to the string the API has always answered.

A request body is closed too (``extra='forbid'``): the declared fields are
exactly the ones the route accepts, so an undeclared or wrongly typed field is a
validation failure (422) instead of being silently dropped. The models stay
loose where the database is the real authority (blank names, a name or filename
over the column limit, an unsupported MIME type) so those keep answering the
owned 400/415 in the route.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

#: Refuse a body that names a field the route does not accept.
_REJECT_EXTRA = ConfigDict(extra='forbid')


# -- requests ----------------------------------------------------------------


class KnowledgeBaseCreateRequest(BaseModel):
    """``POST /api/knowledge-base``."""

    model_config = _REJECT_EXTRA

    name: str = Field(description='Knowledge base name; blank or over 200 characters is refused.')


class KnowledgeBaseUpdateRequest(BaseModel):
    """``PATCH /api/knowledge-base/{knowledge_base_id}``.

    ``name`` is optional so an omitted field and an explicit ``null`` are told
    apart: only the keys the caller sent are read, and an explicit null is left
    for the repository to refuse.
    """

    model_config = _REJECT_EXTRA

    name: str | None = Field(default=None, description='New name; blank or over 200 characters is refused.')


class DocumentCreateRequest(BaseModel):
    """``POST /api/knowledge-base/{knowledge_base_id}/document``.

    ``mime_type`` stays a free string: the accepted set is the route's own
    check, so an unsupported type is answered with 415, not a schema failure.
    """

    model_config = _REJECT_EXTRA

    filename: str = Field(description='Display name; blank or over 255 characters is refused.')
    mime_type: str = Field(description='``application/pdf`` or ``text/plain``.')


# -- responses ---------------------------------------------------------------

#: The eight live states of a document, mirroring the column's CHECK constraint.
#: ``needs_ocr`` is an ``error_code``, not a state.
DocumentState = Literal[
    'pending_upload',
    'uploading',
    'queued',
    'processing',
    'ready',
    'failed',
    'unsupported',
    'deleted',
]


class KnowledgeBaseOut(BaseModel):
    id: UUID
    name: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class KnowledgeBaseListOut(BaseModel):
    knowledge_bases: list[KnowledgeBaseOut]
    limit: int
    offset: int


class KnowledgeBaseCreatedOut(BaseModel):
    knowledge_base: KnowledgeBaseOut


class DocumentOut(BaseModel):
    id: UUID
    knowledge_base_id: UUID
    filename: str
    mime_type: str | None = None
    size_bytes: int | None = None
    state: DocumentState
    attempts: int = 0
    error_code: str | None = None
    error_message: str | None = None
    page_count: int | None = None
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DocumentListOut(BaseModel):
    documents: list[DocumentOut]
    limit: int
    offset: int


class DocumentCreatedOut(BaseModel):
    """The public document projection create, upload and retry all answer with."""

    document: DocumentOut


class DocumentChunkOut(BaseModel):
    """One derived text chunk: the text and where it sits, never its storage row."""

    page_no: int
    ordinal: int
    text: str
    chunk_type: str
    heading_level: int | None = None


class DocumentContentOut(BaseModel):
    document: DocumentOut
    chunks: list[DocumentChunkOut]
    next_offset: int | None = Field(
        default=None, description='Offset of the next page, or null when the document ends here.'
    )


class SearchHitOut(BaseModel):
    """One full-text hit. ``text`` is verbatim, including any HTML."""

    document_id: UUID
    knowledge_base_id: UUID
    filename: str
    version: int
    page_no: int
    ordinal: int
    text: str
    chunk_type: str
    heading_level: int | None = None


class SearchPageOut(BaseModel):
    hits: list[SearchHitOut]
    next_offset: int | None = Field(default=None, description='Offset of the next page, or null at the end.')
