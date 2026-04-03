"""Persistent sync job queue and task-state serialization."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json


def _utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _normalize_dict(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {key: _normalize_value(item) for key, item in value.items()}


def _normalize_list(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not items:
        return []
    return [_normalize_dict(item) or {} for item in items]


@dataclass
class SyncJobState:
    task_id: str
    status: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    group_id: int | None = None
    biz_list: tuple[str, ...] | None = None
    trigger_type: str = 'manual'
    phase: str | None = None
    accounts_total: int = 0
    accounts_done: int = 0
    current_account: dict[str, Any] | None = None
    current_article: dict[str, Any] | None = None
    last_log: str | None = None
    report: dict[str, Any] | None = None
    accounts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            'task_id': self.task_id,
            'status': self.status,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'error': self.error,
            'group_id': self.group_id,
            'phase': self.phase,
            'accounts_total': self.accounts_total,
            'accounts_done': self.accounts_done,
            'current_account': self.current_account,
            'current_article': self.current_article,
            'last_log': self.last_log,
            'report': self.report,
            'accounts': list(self.accounts),
        }

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            'task_id': self.task_id,
            'status': self.status,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'error': self.error,
            'group_id': self.group_id,
            'phase': self.phase,
            'accounts_total': self.accounts_total,
            'accounts_done': self.accounts_done,
            'current_account': self.current_account,
            'current_article': self.current_article,
            'last_log': self.last_log,
        }


def _row_to_state(row: dict[str, Any] | None) -> SyncJobState | None:
    if not row:
        return None
    biz_list = row.get('biz_list')
    accounts = row.get('accounts')
    return SyncJobState(
        task_id=str(row['id']),
        status=str(row['status']),
        created_at=str(_normalize_value(row['created_at'])),
        started_at=_normalize_value(row.get('started_at')),
        finished_at=_normalize_value(row.get('finished_at')),
        error=row.get('error'),
        group_id=row.get('group_id'),
        biz_list=tuple(str(item) for item in biz_list) if isinstance(biz_list, list) else None,
        trigger_type=str(row.get('trigger_type') or 'manual'),
        phase=row.get('phase'),
        accounts_total=int(row.get('accounts_total') or 0),
        accounts_done=int(row.get('accounts_done') or 0),
        current_account=_normalize_dict(row.get('current_account')),
        current_article=_normalize_dict(row.get('current_article')),
        last_log=row.get('last_log'),
        report=_normalize_dict(row.get('report')),
        accounts=_normalize_list(accounts if isinstance(accounts, list) else []),
    )


class SyncJobRepository:
    def __init__(self, conn) -> None:
        self._conn = conn

    def create_job(
        self,
        *,
        trigger_type: str,
        group_id: int | None = None,
        biz_list: list[str] | None = None,
    ) -> SyncJobState:
        task_id = uuid.uuid4().hex
        now = _utc_now_dt()
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO sync_jobs (
                    id,
                    status,
                    trigger_type,
                    group_id,
                    biz_list,
                    accounts,
                    created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    task_id,
                    'queued',
                    trigger_type,
                    group_id,
                    Json(biz_list) if biz_list is not None else None,
                    Json([]),
                    now,
                ),
            )
            row = cur.fetchone()
        if not row:
            raise RuntimeError('Failed to create sync job')
        return _row_to_state(dict(row))  # type: ignore[arg-type]

    def list_jobs(self, *, limit: int = 5) -> list[SyncJobState]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT *
                FROM sync_jobs
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (max(int(limit), 1),),
            )
            rows = cur.fetchall()
        return [_row_to_state(dict(row)) for row in rows if row]  # type: ignore[arg-type]

    def get_job(self, task_id: str) -> SyncJobState | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute('SELECT * FROM sync_jobs WHERE id = %s', (task_id,))
            row = cur.fetchone()
        return _row_to_state(dict(row)) if row else None  # type: ignore[arg-type]

    def has_active_job(self, *, trigger_type: str | None = None) -> bool:
        query = "SELECT 1 FROM sync_jobs WHERE status IN ('queued', 'running')"
        params: list[Any] = []
        if trigger_type:
            query += ' AND trigger_type = %s'
            params.append(trigger_type)
        query += ' LIMIT 1'
        with self._conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone() is not None

    def claim_next_job(self, *, worker_id: str) -> SyncJobState | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                WITH candidate AS (
                    SELECT id
                    FROM sync_jobs
                    WHERE status = 'queued'
                      AND (locked_at IS NULL OR locked_at < NOW() - INTERVAL '15 minutes')
                    ORDER BY created_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE sync_jobs j
                SET locked_by = %s,
                    locked_at = NOW()
                FROM candidate
                WHERE j.id = candidate.id
                RETURNING j.*
                """,
                (worker_id,),
            )
            row = cur.fetchone()
        return _row_to_state(dict(row)) if row else None  # type: ignore[arg-type]

    def mark_running(self, task_id: str, *, worker_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sync_jobs
                SET status = 'running',
                    started_at = COALESCE(started_at, clock_timestamp()),
                    locked_by = %s,
                    locked_at = clock_timestamp()
                WHERE id = %s
                """,
                (worker_id, task_id),
            )

    def update_progress(
        self,
        task_id: str,
        *,
        phase: str | None,
        accounts_total: int,
        accounts_done: int,
        current_account: dict[str, Any] | None,
        current_article: dict[str, Any] | None,
        last_log: str | None,
        accounts: list[dict[str, Any]],
        report: dict[str, Any] | None = None,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sync_jobs
                SET phase = %s,
                    accounts_total = %s,
                    accounts_done = %s,
                    current_account = %s,
                    current_article = %s,
                    last_log = %s,
                    accounts = %s,
                    report = %s
                WHERE id = %s
                """,
                (
                    phase,
                    accounts_total,
                    accounts_done,
                    Json(current_account) if current_account is not None else None,
                    Json(current_article) if current_article is not None else None,
                    last_log,
                    Json(accounts),
                    Json(report) if report is not None else None,
                    task_id,
                ),
            )

    def mark_finished(
        self,
        task_id: str,
        *,
        status: str,
        error: str | None,
        result: dict[str, Any] | None,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sync_jobs
                SET status = %s,
                    error = %s,
                    report = %s,
                    phase = NULL,
                    current_account = NULL,
                    current_article = NULL,
                    finished_at = clock_timestamp(),
                    locked_by = NULL,
                    locked_at = NULL
                WHERE id = %s
                """,
                (
                    status,
                    error,
                    Json(result) if result is not None else None,
                    task_id,
                ),
            )


__all__ = ['SyncJobRepository', 'SyncJobState']
