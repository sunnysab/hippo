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
                source=item.get('source') or '',
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
        source=str(row['source']) if row.get('source') else None,
        scope_sources=tuple(row['scope_sources']) if row.get('scope_sources') is not None else None,
        trigger_type=str(row.get('trigger_type') or 'manual'),
        requester_user_id=int(row['requester_user_id']) if row.get('requester_user_id') is not None else None,
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

    async def create_job(
        self,
        *,
        trigger_type: str,
        group_id: int | None = None,
        biz_list: list[str] | None = None,
        source: str | None = None,
        requester_user_id: int | None = None,
        dedupe: bool = False,
    ) -> SyncTaskState:
        """Insert a job, optionally deduping active manual jobs by requester.

        ``dedupe`` relies on the partial unique index
        ``idx_sync_jobs_active_manual_requester``: a second concurrent request from
        the same user conflicts and gets the already-active job back instead of
        queueing a duplicate. Atomic in the database, not in the caller.
        """
        task_id = uuid.uuid4().hex
        now = utc_now_dt()
        deduping = dedupe and requester_user_id is not None
        conflict = 'ON CONFLICT DO NOTHING' if deduping else ''
        async with self._conn.cursor(row_factory=dict_row) as cur:
            if trigger_type == 'backfill' and biz_list and len(biz_list) == 1:
                # All enqueuers hold the account lock until their transaction commits.
                await cur.execute('SELECT biz FROM accounts WHERE biz = %s FOR UPDATE', (biz_list[0],))
                existing = await self.get_active_backfill(biz_list[0])
                if existing:
                    return existing
            await cur.execute(
                f"""
                INSERT INTO sync_jobs (
                    id,
                    status,
                    trigger_type,
                    group_id,
                    biz_list,
                    source,
                    requester_user_id,
                    accounts,
                    created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                {conflict}
                RETURNING *
                """,
                (
                    task_id,
                    'queued',
                    trigger_type,
                    group_id,
                    Json(biz_list) if biz_list is not None else None,
                    source,
                    requester_user_id,
                    Json([]),
                    now,
                ),
            )
            row = await cur.fetchone()
            if not row and deduping:
                await cur.execute(
                    """
                    SELECT * FROM sync_jobs
                    WHERE trigger_type = 'manual'
                      AND requester_user_id = %s
                      AND status IN ('queued', 'running', 'cancelling')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (requester_user_id,),
                )
                row = await cur.fetchone()
        if not row:
            raise RuntimeError('Failed to create sync job')
        return _row_to_state(dict(row))  # type: ignore[arg-type]

    async def get_active_backfill(self, biz: str) -> SyncTaskState | None:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM sync_jobs WHERE trigger_type = 'backfill' "
                "AND status IN ('queued', 'running', 'cancelling') "
                'AND biz_list @> jsonb_build_array(%s::text) ORDER BY created_at LIMIT 1',
                (biz,),
            )
            row = await cur.fetchone()
        return _row_to_state(dict(row)) if row else None

    async def list_jobs(
        self,
        *,
        limit: int = 5,
        source: str | None = None,
        requester_user_id: int | None = None,
        active: bool | None = None,
        detail: bool = True,
    ) -> list[SyncTaskState]:
        """Recent jobs, optionally narrowed to one source and/or requester."""
        columns = (
            '*'
            if detail
            else """
            id, status, trigger_type, group_id, source, requester_user_id,
            created_at, started_at, finished_at, error, phase, accounts_total,
            accounts_done, current_account, current_article, last_log,
            jsonb_build_object('total_saved', COALESCE(
                (report->>'total_saved')::bigint,
                (SELECT SUM((item->>'saved')::bigint) FROM jsonb_array_elements(accounts) item),
                0)) AS report
        """
        )
        params: list[Any] = []
        where: list[str] = []
        if requester_user_id is not None:
            where.append('requester_user_id = %s')
            params.append(requester_user_id)
        if active is not None:
            operator = 'IN' if active else 'NOT IN'
            where.append(f"status {operator} ('queued', 'running', 'cancelling')")
        filters = (' AND ' + ' AND '.join(where)) if where else ''
        limit = max(int(limit), 1)
        if source:
            # Historical jobs did not always record ``source``. Attribute one
            # only when its stored execution scope proves a single source; an
            # unknown whole-site job belongs on the overview, never on every
            # source page. Do not infer history from today's group membership.
            legacy = """(
                scope_sources = ARRAY[%s]::text[]
                OR (
                    scope_sources IS NULL AND (
                        (
                            jsonb_typeof(accounts) = 'array'
                            AND jsonb_array_length(accounts) > 0
                            AND EXISTS (
                                SELECT 1 FROM jsonb_array_elements(accounts) item
                                WHERE item->>'source' = %s
                            )
                            AND NOT EXISTS (
                                SELECT 1 FROM jsonb_array_elements(accounts) item
                                WHERE item->>'source' IS DISTINCT FROM %s
                            )
                        )
                        OR (
                            jsonb_typeof(biz_list) = 'array'
                            AND jsonb_array_length(biz_list) > 0
                            AND EXISTS (
                                SELECT 1 FROM jsonb_array_elements_text(biz_list) b
                                JOIN accounts a ON a.biz = b.value
                                WHERE a.source = %s
                            )
                            AND jsonb_array_length(biz_list) = (
                                SELECT COUNT(*)
                                FROM jsonb_array_elements_text(biz_list) b
                                JOIN accounts a ON a.biz = b.value
                                WHERE a.source = %s
                            )
                        )
                    )
                )
            )"""
            query = (
                f'(SELECT {columns} FROM sync_jobs WHERE source = %s{filters} ORDER BY created_at DESC LIMIT %s) '
                'UNION ALL '
                f'(SELECT {columns} FROM sync_jobs WHERE source IS NULL{filters} AND {legacy} '
                'ORDER BY created_at DESC LIMIT %s) '
                'ORDER BY created_at DESC LIMIT %s'
            )
            params = [
                source,
                *params,
                limit,
                *params,
                source,
                source,
                source,
                source,
                source,
                limit,
                limit,
            ]
        else:
            query = f'SELECT {columns} FROM sync_jobs'
            if where:
                query += ' WHERE ' + ' AND '.join(where)
            query += ' ORDER BY created_at DESC LIMIT %s'
            params.append(limit)
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()
        return [_row_to_state(dict(row)) for row in rows if row]  # type: ignore[arg-type]

    async def get_job(self, task_id: str) -> SyncTaskState | None:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute('SELECT * FROM sync_jobs WHERE id = %s', (task_id,))
            row = await cur.fetchone()
        return _row_to_state(dict(row)) if row else None  # type: ignore[arg-type]

    async def has_active_job(self, *, trigger_type: str | None = None, source: str | None = None) -> bool:
        query = "SELECT 1 FROM sync_jobs WHERE status IN ('queued', 'running', 'cancelling')"
        params: list[Any] = []
        if trigger_type:
            query += ' AND trigger_type = %s'
            params.append(trigger_type)
        if source:
            query += ' AND source = %s'
            params.append(source)
        query += ' LIMIT 1'
        async with self._conn.cursor() as cur:
            await cur.execute(query, params)
            return await cur.fetchone() is not None

    async def claim_next_job(self, *, worker_id: str) -> SyncTaskState | None:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            # The caller commits claim and running status together. Serialize scope checks.
            await cur.execute("SELECT pg_advisory_xact_lock(hashtext('hippo.sync.claim'))")
            await cur.execute(
                """
                WITH scopes AS MATERIALIZED (
                    SELECT j.id, j.status, j.created_at, j.locked_at,
                           COALESCE(j.scope_sources,
                               CASE WHEN j.source IS NOT NULL THEN ARRAY[j.source]
                               ELSE ARRAY(SELECT DISTINCT a.source FROM accounts a
                                   WHERE (j.biz_list IS NULL OR j.biz_list @> jsonb_build_array(a.biz))
                                     AND (j.group_id IS NULL OR EXISTS (
                                         SELECT 1 FROM subscription s WHERE s.biz = a.biz
                                         AND s.group_id = j.group_id))) END) AS sources
                    FROM sync_jobs j WHERE j.status IN ('queued', 'running', 'cancelling')
                ), candidate AS (
                    SELECT job.id FROM sync_jobs job JOIN scopes q ON q.id = job.id
                    WHERE job.status = 'queued' AND job.locked_at IS NULL
                      AND NOT EXISTS (SELECT 1 FROM scopes active
                          WHERE active.id <> q.id
                            AND (active.status IN ('running', 'cancelling') OR active.locked_at IS NOT NULL)
                            AND active.sources && q.sources)
                    ORDER BY job.created_at ASC
                    FOR UPDATE OF job SKIP LOCKED LIMIT 1
                )
                UPDATE sync_jobs j
                SET locked_by = %s,
                    locked_at = NOW(),
                    scope_sources = scopes.sources
                FROM candidate JOIN scopes ON scopes.id = candidate.id
                WHERE j.id = candidate.id
                RETURNING j.*
                """,
                (worker_id,),
            )
            row = await cur.fetchone()
        return _row_to_state(dict(row)) if row else None  # type: ignore[arg-type]

    async def mark_running(self, task_id: str, *, worker_id: str) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
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

    async def heartbeat_jobs(self, *, worker_id: str) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                'UPDATE sync_jobs SET locked_at = clock_timestamp() '
                "WHERE locked_by = %s AND status IN ('running', 'cancelling')",
                (worker_id,),
            )

    async def release_interrupted_job(self, task_id: str, *, worker_id: str) -> None:
        """Return only this worker's unfinished job; preserve explicit cancellation."""
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE sync_jobs
                SET status = CASE WHEN status = 'cancelling' THEN 'cancelled' ELSE 'queued' END,
                    phase = NULL, current_account = NULL, current_article = NULL,
                    locked_by = NULL, locked_at = NULL, scope_sources = NULL,
                    finished_at = CASE WHEN status = 'cancelling' THEN clock_timestamp() ELSE NULL END
                WHERE id = %s AND locked_by = %s AND status IN ('running', 'cancelling')
                """,
                (task_id, worker_id),
            )

    async def recover_stale_running_jobs(self, *, stale_after_minutes: int = 15) -> int:
        # A stale ``cancelling`` job is cancelled, not failed: it has already been
        # asked to stop, and both statuses must leave the active window or the
        # partial unique index keeps the requester's next manual run out.
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE sync_jobs
                SET status = CASE status
                        WHEN 'cancelling' THEN 'cancelled'
                        ELSE 'failed'
                    END,
                    error = CASE status
                        WHEN 'cancelling' THEN error
                        ELSE 'worker_stopped'
                    END,
                    phase = NULL,
                    current_account = NULL,
                    current_article = NULL,
                    finished_at = clock_timestamp(),
                    locked_by = NULL,
                    locked_at = NULL
                WHERE status IN ('running', 'cancelling')
                  AND locked_at < NOW() - (%s * INTERVAL '1 minute')
                """,
                (max(int(stale_after_minutes), 1),),
            )
            return int(cur.rowcount or 0)

    async def update_progress(
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
        async with self._conn.cursor() as cur:
            await cur.execute(
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

    async def cancel_job(self, task_id: str) -> bool:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
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
            row = await cur.fetchone()
        return row is not None

    async def is_cancelling(self, task_id: str) -> bool:
        async with self._conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM sync_jobs WHERE id = %s AND status = 'cancelling'",
                (task_id,),
            )
            return await cur.fetchone() is not None

    async def mark_finished(
        self,
        task_id: str,
        *,
        status: str,
        error: str | None,
        result: dict[str, Any] | None,
    ) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
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
