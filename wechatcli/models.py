"""Dataclasses used by the CLI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass(slots=True)
class AccountCredential:
    biz: str
    nickname: str
    alias: Optional[str] = None
    round_head_img: Optional[str] = None
    uin: str = ""
    key: str = ""
    pass_ticket: str = ""
    is_default: bool = False
    last_synced_at: Optional[datetime] = None


@dataclass(slots=True)
class ArticleRecord:
    biz: str
    article_id: str
    title: str
    author: Optional[str]
    digest: Optional[str]
    cover: Optional[str]
    link: str
    source_url: Optional[str]
    publish_at: Optional[int]
    raw: Dict[str, Any]


@dataclass(slots=True)
class DownloadResult:
    article: ArticleRecord
    output_path: str
    asset_count: int


@dataclass(slots=True)
class LoginSession:
    token: str
    cookies: Dict[str, str]
    nickname: Optional[str] = None
    avatar: Optional[str] = None


__all__ = ["AccountCredential", "ArticleRecord", "DownloadResult", "LoginSession"]
