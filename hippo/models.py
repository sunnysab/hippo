"""Models used by the CLI."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class HippoBaseModel(BaseModel):
    model_config = ConfigDict(extra='ignore')


class AccountCredential(HippoBaseModel):

    biz: str
    nickname: str
    alias: str | None = None
    round_head_img: str | None = None
    is_disabled: bool = False
    last_synced_at: datetime | None = None
    sync_mode: str | None = None
    sync_recent_days: int | None = None
    group_id: int | None = None
    group_name: str | None = None


class AccountGroup(HippoBaseModel):

    id: int
    name: str
    account_count: int = 0
    sync_mode: str | None = None
    sync_recent_days: int | None = None


class ArticleRecord(HippoBaseModel):

    biz: str
    article_id: str
    title: str
    author: str | None
    digest: str | None
    cover: str | int | None
    link: str
    source_url: str | None
    publish_at: int | None
    raw: dict[str, Any]


class DownloadResult(HippoBaseModel):

    article: ArticleRecord
    asset_count: int


class LoginSession(HippoBaseModel):

    token: str
    cookies: dict[str, str]
    nickname: str | None = None
    avatar: str | None = None


__all__ = [
    "AccountCredential",
    "AccountGroup",
    "ArticleRecord",
    "DownloadResult",
    "LoginSession",
]
