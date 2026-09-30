"""Key/value metadata access."""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row


class MetaRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    async def get(self, key: str) -> str | None:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute('SELECT value FROM meta WHERE key = %s', (key,))
            row = await cur.fetchone()
            return row['value'] if row else None

    async def set(self, key: str, value: str) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(
                'INSERT INTO meta(key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value',
                (key, value),
            )

    async def delete(self, key: str) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute('DELETE FROM meta WHERE key = %s', (key,))
