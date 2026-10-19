"""Request and response models for the sync endpoints.

The task models mirror ``SyncTaskState`` field for field, so the OpenAPI schema
shows the real response instead of an opaque object. Fields absent from a
summary dict stay absent in the response because the routes serialize with
``response_model_exclude_unset=True``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class SyncRunRequest(BaseModel):
    """Body of ``POST /api/settings/run``.

    ``group_id``, ``source`` and ``biz_list`` narrow the run and combine as an
    intersection. An empty body syncs every enabled subscription the caller owns;
    it never falls back to a whole-site run. ``biz_list`` may not be empty.

    Unknown keys are rejected rather than ignored: a typo would otherwise widen
    the run into "all of my subscriptions" silently.
    """

    model_config = ConfigDict(extra='forbid')

    group_id: int | None = None
    source: str | None = None
    biz_list: list[str] | None = None


class SyncSiteRunRequest(BaseModel):
    """Body of ``POST /api/settings/admin/run``: one catalogue-wide run."""

    model_config = ConfigDict(extra='forbid')

    source: str | None = None


class SyncRunResponse(BaseModel):
    """Accepted sync task queued by ``POST /api/settings/run``.

    ``scope`` says which accounts the task covers: ``own`` is the caller's
    subscriptions, ``site`` is the whole catalogue (admin route only). When an
    identical active task already exists its id/status are echoed back.
    """

    task_id: str
    status: str
    scope: Literal['own', 'site']
    group_id: int | None = None
    biz_list: list[str] | None = None
    source: str | None = None


class SyncAccountProgress(BaseModel):
    """One account's slice of a running task, as shown by the reader."""

    model_config = ConfigDict(extra='ignore')

    biz: str
    nickname: str
    source: str = ''
    status: str = 'pending'
    phase: str | None = None
    saved: int = 0
    page_count: int = 0
    article_current: int | None = None
    article_total: int | None = None
    last_article: dict[str, Any] | None = None
    skip_reason: str | None = None
    error: str | None = None
    updated_at: str | None = None


class SyncTaskSummary(BaseModel):
    """A task without its per-account breakdown (``detail=false``)."""

    model_config = ConfigDict(extra='ignore')

    task_id: str
    status: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    group_id: int | None = None
    source: str | None = None
    requester_user_id: int | None = None
    phase: str | None = None
    accounts_total: int = 0
    accounts_done: int = 0
    current_account: dict[str, Any] | None = None
    current_article: dict[str, Any] | None = None
    last_log: str | None = None


class SyncTaskDetail(SyncTaskSummary):
    """A task with the full progress/result payload (``detail=true``)."""

    biz_list: list[str] | None = None
    trigger_type: str | None = None
    report: dict[str, Any] | None = None
    accounts: list[SyncAccountProgress] | None = None


class SyncTaskListResponse(BaseModel):
    """Envelope of ``GET /api/settings/tasks``."""

    tasks: list[SyncTaskDetail] = []


__all__ = [
    'SyncAccountProgress',
    'SyncRunRequest',
    'SyncRunResponse',
    'SyncSiteRunRequest',
    'SyncTaskDetail',
    'SyncTaskListResponse',
    'SyncTaskSummary',
]
