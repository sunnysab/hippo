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
    #: 该 biz 属于哪个来源。默认 weixin 只为让既有数据与测试继续工作：新来源必须显式给值，
    #: 否则会被静默标成微信（见 README「数据来源与多源扩展」）。
    source: str = 'weixin'
    alias: str | None = None
    gh_id: str | None = None
    round_head_img: str | None = None
    is_disabled: bool = False
    last_synced_at: datetime | None = None
    sync_mode: str | None = None
    sync_recent_days: int | None = None
    sync_interval_days: int | None = None
    backfill_state: str = 'done'
    backfill_cursor: str | None = None
    group_id: int | None = None
    group_name: str | None = None


class AccountGroup(HippoBaseModel):
    id: int
    name: str
    account_count: int = 0
    article_count: int = 0
    sync_mode: str | None = None
    sync_recent_days: int | None = None


class User(HippoBaseModel):
    id: int
    username: str
    email: str | None = None
    email_verified: bool = False
    role: str = 'user'
    timezone: str = 'Asia/Shanghai'
    is_disabled: bool = False


class ArticleRecord(HippoBaseModel):
    biz: str
    article_id: str
    title: str
    item_show_type: int | None = None
    author: str | None
    digest: str | None
    cover: str | int | None
    link: str
    source_url: str | None
    publish_at: int | None
    #: 音视频的时长（秒）。文字文章为 None，卡片据此决定要不要显示时长徽章。
    duration_seconds: int | None = None
    raw: dict[str, Any]


class DownloadResult(HippoBaseModel):
    article: ArticleRecord
    asset_count: int


__all__ = [
    'AccountCredential',
    'AccountGroup',
    'ArticleRecord',
    'DownloadResult',
    'User',
]
