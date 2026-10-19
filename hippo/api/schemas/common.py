"""Schema pieces shared by more than one domain."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ErrorBody(BaseModel):
    """The body of every failed request, whatever the status.

    ``error`` is copy for a human, ``code`` is what a client branches on; both
    come from ``hippo.api.errors``.
    """

    error: str = Field(description='Human-readable message.')
    code: str = Field(description='Stable machine-readable identifier.')


class RowPayload(BaseModel):
    """Base for a response read straight out of a database row.

    ``extra='allow'`` is load-bearing: the row is the source of truth, and a
    response model must never drop a column it does not know about. The declared
    fields are the ones this API documents and types.
    """

    model_config = ConfigDict(extra='allow')
