"""Models used by the CLI."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AccountCredential(BaseModel):
    model_config = ConfigDict(extra='ignore')

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


class AccountGroup(BaseModel):
    model_config = ConfigDict(extra='ignore')

    id: int
    name: str
    account_count: int = 0
    sync_mode: str | None = None
    sync_recent_days: int | None = None


class ArticleRecord(BaseModel):
    model_config = ConfigDict(extra='ignore')

    biz: str
    article_id: str
    title: str
    author: str | None
    digest: str | None
    cover: str | None
    link: str
    source_url: str | None
    publish_at: int | None
    raw: dict[str, Any]


class DownloadResult(BaseModel):
    model_config = ConfigDict(extra='ignore')

    article: ArticleRecord
    asset_count: int


class LoginSession(BaseModel):
    model_config = ConfigDict(extra='ignore')

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
