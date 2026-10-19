"""Source subscription and credential schemas.

Hippo subscribes to channels and feeds from multiple sources. The separate
``daemon`` schema module owns the WeChat session used by the crawler.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# -- requests ----------------------------------------------------------------


class SubscribeRequest(BaseModel):
    """``POST /api/source/channel``."""

    url: str = Field(description='Video, channel or feed address to subscribe to.')
    group_id: int | None = Field(default=None, description='Target group; defaults to the default group.')


class CredentialRequest(BaseModel):
    """``PUT /api/source/credential/{source}``. An empty secret clears the cookie."""

    secret: str = Field(description='Cookie value for this source; never read back out.')


# -- responses ---------------------------------------------------------------


class SourceCountOut(BaseModel):
    """One entry of the article rail's source filter."""

    source: str
    channels: int = Field(description='Channels the caller follows from this source.')


class SourceListOut(BaseModel):
    sources: list[SourceCountOut]


class SourcePreviewOut(BaseModel):
    """What a pasted URL resolves to, before anything is written."""

    source: str
    biz: str
    nickname: str
    avatar_url: str | None = None
    subscribed: bool = Field(description='Whether the caller already follows this channel.')


class SubscriptionOut(BaseModel):
    """``POST /api/source/channel``: the channel is queued for its first sync."""

    biz: str
    nickname: str
    source: str
    group_id: int


class CredentialStateOut(BaseModel):
    configured: bool = Field(description='Whether the caller stored a credential for this source.')


class CredentialListOut(BaseModel):
    """``GET /api/source/credential``: a flag per source, never a value.

    One entry per source in ``hippo.sources.credentials.SOURCES``.
    """

    bilibili: CredentialStateOut
    youtube: CredentialStateOut


class CredentialProbeOut(BaseModel):
    """Result of asking the source whether a credential works."""

    source: str
    credential: str = Field(description='user / instance / none — which credential answered.')
    ok: bool
    detail: str = Field(description='Human-readable outcome, written by the probe.')
    account: str = Field(description='Account the credential was accepted as, or empty.')
