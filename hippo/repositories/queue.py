"""Pending article-url queue that feeds the body drain."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json


class ArticleQueueRepository:
    """`article_queue`：列表阶段入队，拿到短链后才建 articles 行。

    列表接口只给长链（``chksm``/``sessionid`` 会失效），所以先用 ``(biz, sn)``
    在队列表里去重排队，正文阶段拿到 ``short_link`` 再写 ``articles.link``。
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    async def enqueue_many(self, items: Iterable[dict[str, Any]]) -> int:
        """入队（已存在的 ``(biz, sn)`` 不重置状态）。返回新增条数。"""
        rows = [
            (
                item['biz'],
                item['sn'],
                item.get('appmsg_id'),
                item['long_link'],
                Json(item.get('payload') or {}),
            )
            for item in items
        ]
        if not rows:
            return 0
        async with self._conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO article_queue (biz, sn, appmsg_id, long_link, payload, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (biz, sn) DO NOTHING
                """,
                rows,
            )
            return cur.rowcount

    async def take_pending(self, limit: int, *, biz: str | None = None) -> list[dict[str, Any]]:
        """原子领取一批：直接标 ``processing`` 并返回（不跨网络请求持锁）。"""
        query = (
            "UPDATE article_queue SET state = 'processing', updated_at = NOW() "
            'WHERE id IN (SELECT id FROM article_queue WHERE state = %s'
        )
        params: list[Any] = ['pending']
        if biz:
            query += ' AND biz = %s'
            params.append(biz)
        query += ' ORDER BY id LIMIT %s) RETURNING id, biz, sn, appmsg_id, long_link, payload, attempts'
        params.append(limit)
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            return list(await cur.fetchall())

    async def requeue_stale(self, older_than_secs: int = 900) -> int:
        """把卡在 ``processing`` 的（worker 崩溃）退回 ``pending``。"""
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE article_queue
                   SET state = 'pending', updated_at = NOW(),
                       last_error = COALESCE(last_error, 'worker 中断，已重排')
                 WHERE state = 'processing'
                   AND updated_at < NOW() - make_interval(secs => %s)
                """,
                (older_than_secs,),
            )
            return cur.rowcount

    async def mark_done(self, queue_ids: Iterable[int]) -> int:
        ids = list(queue_ids)
        if not ids:
            return 0
        async with self._conn.cursor() as cur:
            await cur.execute(
                "UPDATE article_queue SET state = 'done', updated_at = NOW() WHERE id = ANY(%s)",
                (ids,),
            )
            return cur.rowcount

    async def mark_failed(
        self,
        queue_ids: Iterable[int],
        *,
        error: str,
        retryable: bool,
        max_attempts: int = 3,
    ) -> int:
        ids = list(queue_ids)
        if not ids:
            return 0
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE article_queue
                   SET attempts = attempts + 1,
                       last_error = %s,
                       retryable = %s,
                       -- 可恢复的回到 pending，但重试到上限就转 failed（不再无限重取）
                       state = CASE WHEN %s AND attempts + 1 < %s THEN 'pending' ELSE 'failed' END,
                       updated_at = NOW()
                 WHERE id = ANY(%s)
                """,
                (error[:1000], retryable, retryable, max_attempts, ids),
            )
            return cur.rowcount

    async def requeue(self, queue_ids: Iterable[int], *, error: str) -> int:
        """会话失效这类基础设施故障：回 `pending` 且**不计 attempts**（不算文章的账）。

        只动 `processing` 的行：同批已 done / failed 的不被拉回来。
        """
        ids = list(queue_ids)
        if not ids:
            return 0
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE article_queue
                   SET state = 'pending', last_error = %s, retryable = TRUE, updated_at = NOW()
                 WHERE id = ANY(%s) AND state = 'processing'
                """,
                (error[:1000], ids),
            )
            return cur.rowcount

    async def stats(self) -> dict[str, int]:
        """各状态计数，四个键恒定存在（缺状态补 0）。"""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute('SELECT state, COUNT(*) AS n FROM article_queue GROUP BY state')
            counts = {str(row['state']): int(row['n']) for row in await cur.fetchall()}
        return {state: counts.get(state, 0) for state in ('pending', 'processing', 'failed', 'done')}

    async def list_failed(self, limit: int = 5) -> list[dict[str, Any]]:
        """最近失败的队列项，附带账号昵称，供运维面板展示。"""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT q.biz, a.nickname, q.sn, q.attempts, q.retryable, q.last_error, q.updated_at
                  FROM article_queue q
                  LEFT JOIN accounts a ON a.biz = q.biz
                 WHERE q.state = 'failed'
                 ORDER BY q.updated_at DESC
                 LIMIT %s
                """,
                (max(int(limit), 1),),
            )
            rows = await cur.fetchall()
        return [
            {
                'biz': row['biz'],
                'nickname': row['nickname'],
                'sn': row['sn'],
                'attempts': row['attempts'],
                'retryable': row['retryable'],
                'last_error': row['last_error'],
                'updated_at': row['updated_at'].isoformat() if row['updated_at'] else None,
            }
            for row in rows
        ]
