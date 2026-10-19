"""LLM-provider administration schemas.

A response names only public fields on purpose, unlike the open ``RowPayload``
used for content rows: the provider row underneath carries the clear-text API
key, and a model that cannot name that field is the guarantee it never reaches
a client. The console sees ``api_key_masked`` instead.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# -- requests ----------------------------------------------------------------


class ProviderCreateRequest(BaseModel):
    """``POST /api/llm/provider``."""

    name: str
    base_url: str = Field(description='OpenAI-compatible base URL, without a trailing slash.')
    api_key: str = Field(description='Stored so it can be replayed to the provider; only ever returned masked.')
    model: str
    is_default: bool = False
    enabled: bool = True


class ProviderUpdateRequest(BaseModel):
    """``PATCH /api/llm/provider/{provider_id}``.

    A field left out — or sent as null — keeps its stored value, and that is
    also how ``api_key`` behaves: the client never sees the key, so it cannot
    echo it back and a blank or absent key leaves the stored one untouched.
    """

    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    is_default: bool | None = None
    enabled: bool | None = None


# -- responses ---------------------------------------------------------------


class ProviderOut(BaseModel):
    """One provider as the console sees it; the stored key is never included."""

    id: int
    name: str
    base_url: str
    api_key_masked: str = Field(description='Masked key: bullets plus the last four characters.')
    model: str
    is_default: bool
    enabled: bool


class ProviderListOut(BaseModel):
    items: list[ProviderOut]


class ProviderTestOut(BaseModel):
    """Result of probing ``GET {base_url}/models``.

    An unreachable provider is a 200 with ``ok: false``: it is a normal answer to
    the question, not a failed request.
    """

    ok: bool
    latency_ms: int | None = None
    error: str | None = None
    models: list[str] | None = None
    model_available: bool | None = Field(
        default=None, description='Whether the configured model is in ``models``; null when unknown.'
    )
