"""Persistent sync job queue and task-state serialization."""

from __future__ import annotations

import uuid
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from .sync_types import AccountProgress, SyncTaskState
from .utils import normalize_value, utc_now_dt


def _normalize_dict(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {key: normalize_value(item) for key, item in value.items()}


def _row_to_state(row: dict[str, Any] | None) -> SyncTaskState | None:
    if not row:
        return None
    biz_list_raw = row.get('biz_list')
    accounts_raw = row.get('accounts') or []
    accounts: dict[str, AccountProgress] = {}
    for item in accounts_raw if isinstance(accounts_raw, list) else []:
        if isinstance(item, dict):
            biz = item.get('biz', '')
            accounts[biz] = AccountProgress(
                biz=biz,
                nickname=item.get('nickname') or biz,
                status=item.get('status') or 'pending',
                phase=item.get('phase'),
                saved=item.get('saved') or 0,
                page_count=item.get('page_count') or 0,
                article_current=item.get('article_current'),
                article_total=item.get('article_total'),
                last_article=item.get('last_article'),
                skip_reason=item.get('skip_reason'),
                error=item.get('error'),
                updated_at=item.get('updated_at'),
            )
    return SyncTaskState(
        task_id=str(row['id']),
        status=str(row['status']),
        created_at=str(normalize_value(row['created_at'])),
        started_at=normalize_value(row.get('started_at')),
        finished_at=normalize_value(row.get('finished_at')),
        error=row.get('error'),
        group_id=row.get('group_id'),
        biz_list=tuple(str(item) for item in biz_list_raw) if isinstance(biz_list_raw, list) else None,
        trigger_type=str(row.get('trigger_type') or 'manual'),
        phase=row.get('phase'),
        accounts_total=int(row.get('accounts_total') or 0),
        accounts_done=int(row.get('accounts_done') or 0),
        current_account=_normalize_dict(row.get('current_account')),
        current_article=_normalize_dict(row.get('current_article')),
        last_log=row.get('last_log'),
        report=_normalize_dict(row.get('report')),
        accounts=accounts,
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
    ) -> SyncTaskState:
        task_id = uuid.uuid4().hex
        now = utc_now_dt()
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

    def list_jobs(self, *, limit: int = 5) -> list[SyncTaskState]:
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

    def get_job(self, task_id: str) -> SyncTaskState | None:
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

    def claim_next_job(self, *, worker_id: str) -> SyncTaskState | None:
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

    def recover_stale_running_jobs(self, *, stale_after_minutes: int = 15) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sync_jobs
                SET status = 'failed',
                    error = 'worker_stopped',
                    phase = NULL,
                    current_account = NULL,
                    current_article = NULL,
                    finished_at = clock_timestamp(),
                    locked_by = NULL,
                    locked_at = NULL
                WHERE status = 'running'
                  AND locked_at < NOW() - (%s * INTERVAL '1 minute')
                """,
                (max(int(stale_after_minutes), 1),),
            )
            return int(cur.rowcount or 0)

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

    def cancel_job(self, task_id: str) -> bool:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE sync_jobs
                SET status = CASE
                        WHEN status = 'queued' THEN 'cancelled'
                        WHEN status = 'running' THEN 'cancelling'
                        ELSE status
                    END,
                    finished_at = CASE
                        WHEN status = 'queued' THEN clock_timestamp()
                        ELSE finished_at
                    END
                WHERE id = %s
                  AND status IN ('queued', 'running')
                RETURNING status
                """,
                (task_id,),
            )
            row = cur.fetchone()
        return row is not None

    def is_cancelling(self, task_id: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM sync_jobs WHERE id = %s AND status = 'cancelling'",
                (task_id,),
            )
            return cur.fetchone() is not None

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


__all__ = ['SyncJobRepository', 'SyncTaskState']
