"""Instance-administration schemas: users, site, media, audit and logs.

Every response here is a closed model: the routes project a fixed field list, and
a model that cannot name a field is the one guarantee a stored secret or an
internal column never reaches a caller. The media settings carry write-only
secrets, so their list view is the mask plus a ``*_set`` companion.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal['user', 'admin']

AsrTier = Literal['accurate', 'fast']

KeepMediaBitrate = Literal['original', '64k', '96k', '128k', '192k', '256k']

# -- requests ----------------------------------------------------------------


class UserCreateRequest(BaseModel):
    """``POST /api/admin/user``: the admin sets the password, so e-mail is optional."""

    username: str
    password: str = Field(description='At least 8 characters.')
    email: str | None = None
    role: Role = 'user'
    email_verified: bool = Field(default=True, description='No verification mail is sent from here.')


class UserUpdateRequest(BaseModel):
    """``PATCH /api/admin/user/{user_id}``: only the keys sent are changed."""

    is_disabled: bool | None = Field(default=None, description='Disabling also revokes every session.')
    role: Role | None = None
    email_verified: bool | None = None
    timezone: str | None = Field(default=None, description='IANA name; empty is refused.')


class PasswordResetRequest(BaseModel):
    """``POST /api/admin/user/{user_id}/password``."""

    password: str = Field(description='At least 8 characters; every session of that user is revoked.')


class SiteSettingsUpdateRequest(BaseModel):
    """``PATCH /api/admin/site``: only the keys sent are changed."""

    registration_enabled: bool | None = Field(
        default=None, description='Stop new accounts; existing ones keep working.'
    )
    site_name: str | None = None
    public_base_url: str | None = Field(default=None, description='Absolute URL used in mails and report links.')


class MediaSettingsUpdateRequest(BaseModel):
    """``PATCH /api/admin/media``: any subset of the media settings.

    The field list is the config whitelist. Secrets are write-only: a field left
    at the mask the console displayed keeps the stored value, an empty string
    clears it.
    """

    enabled: bool | None = None
    asr_tier: AsrTier | None = None
    asr_endpoints: dict[str, str] | None = Field(
        default=None, description='Endpoint per tier; a blank tier is disabled.'
    )
    asr_api_key: str | None = None
    asr_language: str | None = None
    asr_timeout_seconds: int | None = None
    asr_max_new_tokens: int | None = None
    chunk_seconds: int | None = None
    min_tail_seconds: int | None = None
    max_chunk_seconds: int | None = None
    items_per_sync: int | None = Field(default=None, description='How many new items one channel may queue per sync.')
    max_duration_seconds: int | None = None
    transcribe_max_attempts: int | None = None
    keep_media: bool | None = None
    keep_media_bitrate: KeepMediaBitrate | None = None
    bilibili_cc_subtitle: bool | None = None
    bilibili_cookie: str | None = None
    youtube_cookies: str | None = None
    proxy_default: str | None = None
    proxy_weixin: str | None = None
    proxy_bilibili: str | None = None
    proxy_youtube: str | None = None
    proxy_podcast: str | None = None


# -- responses ---------------------------------------------------------------


class AdminUserOut(BaseModel):
    """One account in the admin list, with its live-session count."""

    id: int
    username: str
    email: str | None = None
    email_verified: bool = False
    role: str = 'user'
    timezone: str = 'Asia/Shanghai'
    is_disabled: bool = False
    created_at: str | None = None
    session_count: int = 0
    last_login_at: str | None = None


class AdminUserListOut(BaseModel):
    items: list[AdminUserOut]


class AdminUserCreatedOut(BaseModel):
    id: int
    username: str
    role: str


class AdminUserUpdatedOut(BaseModel):
    id: int
    changes: dict[str, Any] = Field(description='The keys that were written, with their new values.')


class SiteSettingsOut(BaseModel):
    registration_enabled: bool
    site_name: str
    public_base_url: str = Field(description='Empty means links fall back to the request host.')


class MediaSettingsOut(BaseModel):
    """Media and transcription settings as the console sees them.

    Every secret is replaced by ``mask_key``, and its ``*_set`` companion says
    whether a value is stored at all.
    """

    enabled: bool
    asr_tier: str
    asr_endpoints: dict[str, str]
    asr_api_key: str = Field(description='Masked.')
    asr_api_key_set: bool
    asr_language: str
    asr_timeout_seconds: int
    asr_max_new_tokens: int
    chunk_seconds: int
    min_tail_seconds: int
    max_chunk_seconds: int
    items_per_sync: int
    max_duration_seconds: int
    transcribe_max_attempts: int
    keep_media: bool
    keep_media_bitrate: str
    bilibili_cc_subtitle: bool
    bilibili_cookie: str = Field(description='Masked.')
    bilibili_cookie_set: bool
    youtube_cookies: str = Field(description='Masked.')
    youtube_cookies_set: bool
    proxy_default: str
    proxy_weixin: str
    proxy_bilibili: str
    proxy_youtube: str
    proxy_podcast: str


class AsrProbeOut(BaseModel):
    """Reachability of one ASR tier."""

    tier: str
    ok: bool
    detail: str


class MediaTestOut(BaseModel):
    asr: list[AsrProbeOut]
    ok: bool = Field(description='Whether every probed tier answered.')


class AuditEntryOut(BaseModel):
    id: int
    user_id: int | None = None
    username: str | None = None
    action: str
    target: str | None = None
    detail: dict[str, Any] | None = None
    ip: str | None = None
    created_at: str | None = None


class AuditPageOut(BaseModel):
    items: list[AuditEntryOut]
    page: int
    page_size: int
    total: int
    pages: int


class AuditActionsOut(BaseModel):
    items: list[str] = Field(description='Distinct actions present in the trail.')


class LogTailOut(BaseModel):
    """Tail of the rotating file log; the fallback when SignOz is unreachable.

    The unavailable answer only carries ``available``/``reason``/``lines``; the
    available one only ``available``/``path``/``size``/``lines``. The route drops
    the ``None``s so the shape stays exactly that.
    """

    available: bool
    reason: str | None = None
    path: str | None = None
    size: int | None = None
    lines: list[str] = Field(default_factory=list)


class SignozLinkOut(BaseModel):
    available: bool
    reason: str | None = None
    url: str | None = None
    since: str | None = None
    until: str | None = None


class SourceBacklogOut(BaseModel):
    pending: int
    processing: int
    failed: int
    done: int


class SourceCredentialOut(BaseModel):
    field: str = Field(description="Instance setting that holds this source's credential.")
    configured: bool


class SourceOverviewOut(BaseModel):
    """One source as the admin page renders it.

    The list is assembled server-side on purpose: which sources exist and which
    one takes a credential are backend facts, not a copy kept in the browser.
    """

    source: str
    channels: int = Field(description='Catalogue rows from this source.')
    enabled_channels: int
    followers: int = Field(description='Subscriptions across every user.')
    last_synced_at: str | None = None
    credential: SourceCredentialOut | None = Field(default=None, description='Null for sources without one.')
    proxy: str = Field(description='Proxy in effect, already resolved against the instance default.')
    backlog: SourceBacklogOut | None = None


class SourceOverviewListOut(BaseModel):
    sources: list[SourceOverviewOut]
    media_enabled: bool
