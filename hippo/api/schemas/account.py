"""Group and subscribed-account schemas.

A group is a folder inside one user's subscription list. An account is the
shared catalogue row (nickname, alias, avatar, source) joined with the caller's
own subscription (group, disabled flag, sync interval), which is why the same
``biz`` can look different to two users.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from ...models import AccountGroup

# -- requests ----------------------------------------------------------------


class GroupCreateRequest(BaseModel):
    """``POST /api/group``."""

    name: str = Field(description='Group name, unique per user.')


class GroupUpdateRequest(BaseModel):
    """``PATCH /api/group/{group_id}``."""

    name: str = Field(description='New group name.')


class AccountCreateRequest(BaseModel):
    """``POST /api/account``: add a WeChat public account to the catalogue."""

    biz: str = Field(description='``gh_…`` id from the search endpoint, or a ``Mz…==`` fakeid.')
    nickname: str = Field(description='Display name.')
    alias: str | None = Field(default=None, description='WeChat id of the account.')
    round_head_img: str | None = Field(default=None, description='Avatar URL gathered from the search result.')
    group_id: int | None = Field(default=None, description="Target group; defaults to the caller's default group.")


class AccountUpdateRequest(BaseModel):
    """``PATCH /api/account/{biz}``.

    Catalogue fields (``nickname``/``alias``/``round_head_img``) are shared, the
    rest belong to the caller's own subscription.
    """

    nickname: str | None = None
    alias: str | None = None
    round_head_img: str | None = None
    group_id: int | None = Field(default=None, description='Move the subscription into this group.')
    is_disabled: bool | None = Field(default=None, description='Stop syncing this account for this user.')
    sync_interval_days: int | None = Field(
        default=None, description='Days between syncs; 1 or more, or null for the group default.'
    )


class AccountMoveRequest(BaseModel):
    """``POST /api/account/move``."""

    biz_list: list[str] = Field(description='Accounts to move.')
    group_id: int | None = Field(default=None, description='Target group; defaults to the default group.')


class AccountBatchRequest(BaseModel):
    """``POST /api/account/batch``: one sync setting over many accounts."""

    biz_list: list[str] = Field(description='Accounts to update.')
    sync_interval_days: int | None = Field(default=None, description='Days between syncs, 1 or more.')


# -- responses ---------------------------------------------------------------


class AccountOut(BaseModel):
    """One subscribed account, as the list and detail return it.

    Every field is one the account query selects: this is the public shape of
    that row, not a copy of it.
    """

    biz: str
    nickname: str
    source: str = Field(description='Where the account came from, e.g. ``weixin``.')
    alias: str = Field(description='Empty string when the account has no visible id.')
    round_head_img: str | None = None
    group_id: int | None = None
    group_name: str | None = None
    is_disabled: bool = False
    last_synced_at: datetime | None = Field(default=None, description='Last time the sync touched this account.')
    latest_publish_at: datetime | None = Field(
        default=None, description='Publication time of the latest stored work; null when unavailable.'
    )
    sync_mode: str | None = None
    sync_recent_days: int | None = None
    sync_interval_days: int | None = None
    article_count: int = 0
    avatar_ready: bool = False
    avatar_url: str = Field(description='Same-origin URL of the cached avatar.')
    backfill_pending: bool = False
    backfill_running: bool = False


class AccountListOut(BaseModel):
    accounts: list[AccountOut]
    page: int
    page_size: int
    total: int


class AccountCreatedOut(BaseModel):
    """``POST /api/account``: the catalogue row that was written.

    Narrower than :class:`AccountOut` on purpose: creating subscribes the caller
    and returns what was stored, while the sync state (interval, counters,
    backfill flags) is only known once the account is read back as a
    subscription.
    """

    biz: str
    nickname: str
    alias: str
    round_head_img: str | None = None
    group_id: int | None = None


class AccountSearchResultOut(BaseModel):
    """One candidate from the WeChat search endpoint, before it is subscribed."""

    biz: str = Field(description='``gh_…`` id; the catalogue biz is resolved on subscribe.')
    nickname: str
    alias: str
    round_head_img: str
    is_added: bool = Field(description='Whether the caller already follows a matching account.')
    avatar_url: str = Field(description='Same-origin avatar URL, or empty when there is no picture.')


class AccountSearchOut(BaseModel):
    results: list[AccountSearchResultOut]
    page: int
    page_size: int
    total: int = Field(description='Size of this page; the upstream search has no grand total.')


class UpdatedCountOut(BaseModel):
    updated: int


class GroupListOut(BaseModel):
    """``GET /api/group``: the group list also carries the default group id."""

    default_group_id: int
    groups: list[AccountGroup]
