"""Persistence for personal ASR correction rules."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

_COLUMNS = 'id, source_text, replacement_text, account_biz, enabled'


class CorrectionRepository:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def lock_user(self, user_id: int) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute('SELECT id FROM users WHERE id = %s FOR UPDATE', (user_id,))

    async def valid_scope(self, user_id: int, account_biz: list[str]) -> bool:
        if not account_biz:
            return True
        async with self._conn.cursor() as cur:
            await cur.execute(
                'SELECT biz FROM subscription WHERE user_id = %s AND biz = ANY(%s) AND NOT is_disabled FOR SHARE',
                (user_id, account_biz),
            )
            return len(await cur.fetchall()) == len(account_biz)

    async def list_for_user(self, user_id: int) -> list[dict[str, Any]]:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f'SELECT {_COLUMNS} FROM correction_rule WHERE user_id = %s ORDER BY id', (user_id,))
            return [dict(row) for row in await cur.fetchall()]

    async def create(self, user_id: int, data: dict[str, Any]) -> dict[str, Any]:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""INSERT INTO correction_rule (user_id, source_text, replacement_text, account_biz, enabled)
                    VALUES (%s, %s, %s, %s, %s) RETURNING {_COLUMNS}""",
                (user_id, data['source_text'], data['replacement_text'], data['account_biz'], data['enabled']),
            )
            return dict(await cur.fetchone())

    async def update(self, user_id: int, rule_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""UPDATE correction_rule SET source_text = %s, replacement_text = %s,
                    account_biz = %s, enabled = %s WHERE user_id = %s AND id = %s RETURNING {_COLUMNS}""",
                (data['source_text'], data['replacement_text'], data['account_biz'], data['enabled'], user_id, rule_id),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def delete(self, user_id: int, rule_id: int) -> bool:
        async with self._conn.cursor() as cur:
            await cur.execute('DELETE FROM correction_rule WHERE user_id = %s AND id = %s', (user_id, rule_id))
            return cur.rowcount > 0
