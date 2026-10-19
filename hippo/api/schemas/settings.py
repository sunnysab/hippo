"""Sync settings, reading preferences and the global sync status.

The settings object is admin-only and mixes two stores: the sync knobs live in
``sync:settings`` and the SMTP block in ``email:settings``. The SMTP password is
write-only here — the response carries a mask plus a ``smtp_password_set``
companion, and a round-tripped mask is dropped before a write, so the console
can display the form without ever holding the secret.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EmailSettingsOut(BaseModel):
    """The SMTP block as the admin console reads it.

    ``smtp_password`` is the mask, never the stored value; ``smtp_password_set``
    says whether one is stored at all, which is what the form needs to offer a
    "keep the current password" choice.
    """

    model_config = ConfigDict(extra='ignore')

    smtp_host: str = ''
    smtp_port: int = 587
    smtp_user: str = ''
    smtp_password: str = Field(default='', description='Masked.')
    smtp_password_set: bool = False
    smtp_tls: bool = True
    from_email: str = ''


class SyncSettingsOut(BaseModel):
    """``GET /api/settings``: the sync cadence and the SMTP block."""

    model_config = ConfigDict(extra='ignore')

    enabled: bool
    interval_minutes: int
    sleep_seconds: float
    skip_minutes: int
    download_content: bool
    download_images: bool
    article_exclude_keywords: str
    alert_enabled: bool
    alert_email: str
    email: EmailSettingsOut


class EmailSettingsUpdate(BaseModel):
    """The ``email`` sub-object of ``PATCH /api/settings`` and test-email.

    Only the keys sent are written. ``smtp_password`` is write-only: a submitted
    value that still contains the mask is the round-tripped display value and is
    ignored, an empty string clears the stored password.
    """

    model_config = ConfigDict(extra='forbid')

    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=1)
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_tls: bool | None = None
    from_email: str | None = None


class SyncSettingsUpdateRequest(BaseModel):
    """``PATCH /api/settings``: only the keys sent are changed.

    An unknown key is refused (422) rather than ignored: a misspelled cadence
    key would otherwise leave the scheduler on its old value silently.
    """

    model_config = ConfigDict(extra='forbid')

    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=1, description='Minutes between scheduled runs.')
    sleep_seconds: float | None = Field(default=None, ge=0, description='Pause between account requests.')
    skip_minutes: int | None = Field(default=None, ge=0, description='Skip an account synced this recently.')
    download_content: bool | None = None
    download_images: bool | None = None
    article_exclude_keywords: str | None = Field(
        default=None, description='Instance-default article filter; the reader may override it per account.'
    )
    alert_enabled: bool | None = None
    alert_email: str | None = None
    email: EmailSettingsUpdate | None = None


class PreferencesOut(BaseModel):
    """The caller's personal reading preferences."""

    timezone: str
    article_exclude_keywords: str


class PreferencesUpdateRequest(BaseModel):
    """``PATCH /api/settings/preferences``: only the keys sent are changed."""

    model_config = ConfigDict(extra='forbid')

    timezone: str | None = None
    article_exclude_keywords: str | None = None


class EmailTestRequest(BaseModel):
    """``POST /api/settings/test-email``.

    ``to_email`` falls back to the configured ``alert_email`` at runtime, so it
    is optional here. ``email`` overrides the stored SMTP block for this one
    send and is not persisted.
    """

    model_config = ConfigDict(extra='forbid')

    to_email: str | None = None
    email: EmailSettingsUpdate | None = None


class EmailTestResult(BaseModel):
    status: Literal['sent'] = 'sent'
    to_email: str


class QueueStateOut(BaseModel):
    """One queue's per-state counts; a missing state reads as zero."""

    model_config = ConfigDict(extra='ignore')

    pending: int = 0
    processing: int = 0
    failed: int = 0
    done: int = 0


class ImageQueueOut(BaseModel):
    """The image backfill backlog (it only has pending and failed)."""

    model_config = ConfigDict(extra='ignore')

    pending: int = 0
    failed: int = 0


class FailedQueueItemOut(BaseModel):
    """One recently failed article-queue row, as the ops panel shows it."""

    model_config = ConfigDict(extra='ignore')

    biz: str | None = None
    nickname: str | None = None
    sn: str | None = None
    attempts: int | None = None
    retryable: bool | None = None
    last_error: str | None = None
    updated_at: str | None = None


class QueueStatsOut(BaseModel):
    """The worker's queue watermark.

    ``media`` is keyed by source name, which is an open set (the source registry
    decides), so it stays a dictionary rather than a fixed field per source.
    """

    model_config = ConfigDict(extra='ignore')

    articles: QueueStateOut = Field(default_factory=QueueStateOut)
    images: ImageQueueOut = Field(default_factory=ImageQueueOut)
    media: dict[str, QueueStateOut] = Field(default_factory=dict)
    failed_items: list[FailedQueueItemOut] = Field(default_factory=list)


class SyncHistoryEntryOut(BaseModel):
    """One finished run, newest first in ``sync:history``."""

    model_config = ConfigDict(extra='ignore')

    started_at: str
    finished_at: str
    status: str
    error: str = ''
    saved: int = 0
    downloaded: int = 0
    skipped_accounts: int = 0
    failed_accounts: int = 0
    accounts_total: int = 0
    accounts_done: int = 0
    current_account: dict[str, Any] | None = None


class SyncStatusOut(BaseModel):
    """``GET /api/settings/status``: the whole-instance sync view.

    Admin-only, because the queue watermark and worker heartbeat are global;
    a normal user reads their own tasks from ``GET /api/settings/tasks``.
    """

    model_config = ConfigDict(extra='ignore')

    status: str
    last_started_at: str | None = None
    last_finished_at: str | None = None
    last_ingest_at: str | None = None
    last_error: str | None = None
    history: list[SyncHistoryEntryOut] = Field(default_factory=list)
    queue: QueueStatsOut = Field(default_factory=QueueStatsOut)
    worker_heartbeat_at: str | None = None


__all__ = [
    'EmailSettingsOut',
    'EmailSettingsUpdate',
    'EmailTestRequest',
    'EmailTestResult',
    'FailedQueueItemOut',
    'ImageQueueOut',
    'PreferencesOut',
    'PreferencesUpdateRequest',
    'QueueStateOut',
    'QueueStatsOut',
    'SyncHistoryEntryOut',
    'SyncSettingsOut',
    'SyncSettingsUpdateRequest',
    'SyncStatusOut',
]
