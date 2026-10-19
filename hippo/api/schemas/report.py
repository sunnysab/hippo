"""Daily-report schemas.

The report is the one thing hippo mails out: the settings that decide when and
to whom, plus the preview of what one local day contains.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .common import RowPayload


class ReportSettingUpdateRequest(BaseModel):
    """``PATCH /api/report/setting``: only the keys sent are changed.

    A key left out — or sent as null — keeps its stored value, so a partial
    update never resets a preference the caller did not mention.
    """

    enabled: bool | None = Field(default=None, description='Turn the daily mail on or off.')
    send_hour: int | None = Field(default=None, description='Local hour of the day, 0-23.')
    recipients: list[str] | None = Field(
        default=None, description='Addresses to mail; empty falls back to the account e-mail.'
    )
    group_ids: list[int] | None = Field(default=None, description='Groups to include; empty means every group.')
    include_read: bool | None = Field(default=None, description='Include articles the reader already opened.')


class ReportSettingOut(BaseModel):
    """The caller's report preferences, with defaults filled in."""

    enabled: bool
    send_hour: int
    recipients: list[str]
    group_ids: list[int]
    include_read: bool


class ReportArticleOut(RowPayload):
    id: int
    title: str
    digest: str | None = None
    link: str
    author: str | None = None
    publish_at: int | None = None
    cover: int | str | None = None
    biz: str
    nickname: str | None = None
    group_id: int | None = None
    group_name: str | None = None
    display_time: str = Field(default='', description="Publish time in the reader's timezone.")
    display_url: str = Field(default='', description='Deep link into the SPA, when a public base URL is configured.')


class ReportGroupOut(BaseModel):
    """Articles of one group; the ungrouped bucket has ``id`` null and comes last."""

    id: int | None = None
    name: str
    articles: list[ReportArticleOut]


class ReportOut(BaseModel):
    date: str = Field(description='Local date the report covers.')
    timezone: str
    total: int
    groups: list[ReportGroupOut]
    setting: ReportSettingOut


class ReportSendOut(BaseModel):
    """Outcome of ``POST /api/report/{date}/send``.

    Sending twice on the same day is a no-op, and says so, rather than a second
    e-mail.
    """

    sent: bool
    reason: Literal['smtp_not_configured', 'already_delivered', 'send_failed'] | None = None
    error: str | None = None
    recipients: list[str] | None = None
