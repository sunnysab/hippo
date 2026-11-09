"""Read model for account ingestion: list discovery and body work are independent."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

ACTIVE_JOB_STATUSES = ('queued', 'pending', 'running', 'cancelling')


def account_sync_state(row: dict[str, Any]) -> dict[str, Any]:
    """A finished list is not a finished body queue; timestamps retain their meaning."""
    job_status = row.get('history_job_status')
    if row.get('source') != 'weixin':
        history = 'unsupported'
    elif row.get('backfill_state') == 'done':
        history = 'complete'
    elif job_status == 'cancelling':
        history = 'cancelling'
    elif row.get('backfill_state') == 'paused' or not row.get('history_enabled', True):
        history = 'paused'
    elif job_status in ACTIVE_JOB_STATUSES:
        history = 'queued' if job_status == 'pending' else job_status
    elif job_status == 'failed':
        history = 'failed'
    else:
        history = 'pending'
    return {
        'history': {
            'status': history,
            'error': row.get('history_error') if history == 'failed' else None,
            'task_id': row.get('history_task_id'),
        },
        'articles': {key: int(row.get(key) or 0) for key in ('pending', 'processing', 'failed')},
        'listing': row.get('listing'),
        'last_checked_at': row.get('last_synced_at'),
    }


async def enrich_account_sync(conn: Any, accounts: list[dict[str, Any]]) -> None:
    """One bounded query for the visible accounts, using article_queue's (biz, sn) index.

    Active targeted jobs and the currently visited account of a whole-site sweep
    count as listing work. Merely belonging to a sweep is not an active account.
    """
    if not accounts:
        return
    biz_list = [account['biz'] for account in accounts]
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            WITH selected AS MATERIALIZED (
                SELECT * FROM accounts WHERE biz = ANY(%s)
            ), body_counts AS (
                SELECT biz,
                       COUNT(*) FILTER (WHERE state = 'pending') AS pending,
                       COUNT(*) FILTER (WHERE state = 'processing') AS processing,
                       COUNT(*) FILTER (WHERE state = 'failed') AS failed
                FROM article_queue WHERE biz = ANY(%s) AND state <> 'done'
                GROUP BY biz
            ), history_candidates AS MATERIALIZED (
                SELECT id, status, error, created_at, biz_list FROM sync_jobs
                WHERE trigger_type = 'backfill'
                  AND EXISTS (SELECT 1 FROM selected WHERE backfill_state <> 'done')
                  AND biz_list ?| %s
            ), history AS (
                SELECT DISTINCT ON (b.biz) b.biz, j.id, j.status, j.error
                FROM history_candidates j
                CROSS JOIN LATERAL jsonb_array_elements_text(j.biz_list) b(biz)
                JOIN selected a ON a.biz = b.biz
                ORDER BY b.biz, (j.status IN ('queued', 'running', 'cancelling')) DESC, j.created_at DESC
            ), listing_candidates AS MATERIALIZED (
                SELECT id, status, trigger_type, created_at, biz_list, current_account FROM sync_jobs
                WHERE status IN ('queued', 'running', 'cancelling')
            ), listing AS (
                SELECT DISTINCT ON (a.biz) a.biz,
                       jsonb_build_object('task_id', j.id, 'status', j.status, 'kind', j.trigger_type) AS listing
                FROM selected a JOIN listing_candidates j
                  ON j.current_account->>'biz' = a.biz OR j.biz_list @> jsonb_build_array(a.biz)
                ORDER BY a.biz, (j.status IN ('running', 'cancelling')) DESC, j.created_at ASC
            )
            SELECT a.biz, a.source, a.backfill_state, a.last_synced_at,
                   (NOT a.is_disabled AND EXISTS (
                       SELECT 1 FROM subscription s WHERE s.biz = a.biz AND NOT s.is_disabled
                   )) AS history_enabled,
                   q.pending, q.processing, q.failed,
                   h.id AS history_task_id, h.status AS history_job_status, h.error AS history_error,
                   l.listing
            FROM selected a
            LEFT JOIN body_counts q ON q.biz = a.biz
            LEFT JOIN history h ON h.biz = a.biz
            LEFT JOIN listing l ON l.biz = a.biz
            """,
            (biz_list, biz_list, biz_list),
        )
        states = {row['biz']: account_sync_state(dict(row)) for row in await cur.fetchall()}
    for account in accounts:
        account['sync'] = states.get(account['biz'])
