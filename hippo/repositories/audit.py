"""Audit trail: who changed what.

Deliberately separate from the application log. The log goes to SignOz and is
ephemeral; the audit trail is queryable, has business meaning, and is what the
admin console shows.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from ..utils import utc_now_dt


class AuditRepository:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def record(
        self,
        user_id: int | None,
        action: str,
        *,
        target: str | None = None,
        detail: dict[str, Any] | None = None,
        ip: str | None = None,
    ) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO audit_log (user_id, action, target, detail, ip, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (user_id, action, target, Json(detail) if detail is not None else None, ip, utc_now_dt()),
            )

    async def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        action: str | None = None,
        user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._filters(action, user_id)
        params.extend([max(limit, 1), max(offset, 0)])
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT a.id, a.user_id, u.username, a.action, a.target, a.detail, a.ip, a.created_at
                FROM audit_log a
                LEFT JOIN users u ON u.id = a.user_id
                {where}
                ORDER BY a.id DESC
                LIMIT %s OFFSET %s
                """,
                params,
            )
            rows = await cur.fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            created = record.get('created_at')
            record['created_at'] = created.isoformat() if created else None
            result.append(record)
        return result

    async def count(self, *, action: str | None = None, user_id: int | None = None) -> int:
        where, params = self._filters(action, user_id)
        async with self._conn.cursor() as cur:
            await cur.execute(f'SELECT count(*) FROM audit_log a {where}', params)
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _filters(action: str | None, user_id: int | None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if action:
            clauses.append('a.action = %s')
            params.append(action)
        if user_id is not None:
            clauses.append('a.user_id = %s')
            params.append(user_id)
        where = f'WHERE {" AND ".join(clauses)}' if clauses else ''
        return where, params
