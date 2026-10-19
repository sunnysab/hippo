"""Source subscription, credential and daemon-login schemas.

Hippo subscribes to more than WeChat: a pasted URL decides the source
(``weixin``, ``bilibili``, ``youtube``, ``podcast``). ``/api/login/*`` is not
Hippo authentication — it is the WeChat account the crawler runs as.
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
    avatar_url: str = ''
    subscribed: bool = Field(description='Whether the catalogue already has this channel.')


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


class DaemonLoginStatusOut(BaseModel):
    """State of the WeChat daemon session hippo crawls with."""

    logged_in: bool
    status: str = Field(description="online / logged_out / unreachable / the daemon's own word.")
    need_relogin: bool
    wxid: str | None = None
    nickname: str | None = None
    head_url: str | None = None
    clients_connected: int | None = None
    error: str = Field(default='', description='Why the daemon could not be reached, when it could not.')


class DaemonActionResultOut(BaseModel):
    """``POST /api/login/auto`` and ``POST /api/login/wait``."""

    ok: bool
    result: dict[str, object] = Field(description="The daemon's own reply, passed through unchanged.")


class DaemonQrOut(BaseModel):
    """``POST /api/login/qr``: what the console renders so the owner can scan."""

    uuid: str
    url: str = ''
    png_base64: str = Field(description='PNG bytes, base64 encoded; empty when the daemon sent only a URL.')
    expires_in: int = Field(description='Seconds until the QR code goes stale.')
