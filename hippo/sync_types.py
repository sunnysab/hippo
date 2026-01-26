"""Shared sync data structures and observer hooks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


class SyncMode(str, Enum):
    full = 'full'
    incremental = 'incremental'
    recent = 'recent'
    range = 'range'

    def __str__(self) -> str:  # pragma: no cover - click displays value
        return self.value


@dataclass(frozen=True)
class SyncConfig:
    mode: SyncMode | None
    page_size: int
    page_limit: int | None
    sleep_seconds: float
    reset: bool
    recent_days: int | None
    since_date: str | None
    until_date: str | None
    force: bool
    skip_minutes: int | None
    download_content: bool
    download_images: bool
    content_limit: int


@dataclass(frozen=True)
class SyncPlan:
    page_limit: int | None
    since_timestamp: int | None
    until_timestamp: int | None
    stop_on_existing: bool
    full_synced_hint: bool
    resume_key: str | None
    complete_key: str | None


@dataclass(frozen=True)
class SyncSummary:
    total_saved: int
    page_count: int
    completed: bool


@dataclass(frozen=True)
class SyncAccountResult:
    biz: str
    nickname: str
    saved: int
    completed: bool
    skipped: bool
    skip_reason: str | None


@dataclass(frozen=True)
class SyncReport:
    total_saved: int
    summary: list[tuple[str, int]]
    details: list[SyncAccountResult]
    downloaded: int = 0


class SyncObserver(Protocol):
    def on_log(self, message: str) -> None:
        ...

    def on_progress(self, *, current: int | None, total: int | None, delta: int | None) -> None:
        ...

    def on_page(self, payload: dict[str, Any]) -> None:
        ...

    def on_complete(self, summary: SyncSummary) -> None:
        ...

    def on_skip(self, reason: str) -> None:
        ...


class NullSyncObserver:
    def on_log(self, message: str) -> None:
        return None

    def on_progress(self, *, current: int | None, total: int | None, delta: int | None) -> None:
        return None

    def on_page(self, payload: dict[str, Any]) -> None:
        return None

    def on_complete(self, summary: SyncSummary) -> None:
        return None

    def on_skip(self, reason: str) -> None:
        return None


__all__ = [
    'NullSyncObserver',
    'SyncConfig',
    'SyncAccountResult',
    'SyncMode',
    'SyncObserver',
    'SyncPlan',
    'SyncReport',
    'SyncSummary',
]
