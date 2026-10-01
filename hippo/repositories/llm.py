"""LLM providers.

The API key is stored in clear — it has to be replayed to the provider — so the
only protection is never returning it whole. ``mask_key`` is what the API layer
uses to keep the value out of responses.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from ..exceptions import ApiError
from ..utils import utc_now_dt

_COLUMNS = 'id, name, base_url, api_key, model, is_default, enabled'

#: Characters kept when masking. Four is enough to recognise a key, too few to use.
_VISIBLE_TAIL = 4


def mask_key(api_key: str) -> str:
    """Return a display-only form of ``api_key``: ``••••`` plus the last 4 chars."""
    if not api_key:
        return ''
    if len(api_key) <= _VISIBLE_TAIL:
        return '•' * len(api_key)
    return '•' * 4 + api_key[-_VISIBLE_TAIL:]


def _row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': row['id'],
        'name': row['name'],
        'base_url': row['base_url'],
        'api_key_masked': mask_key(row['api_key']),
        'model': row['model'],
        'is_default': bool(row['is_default']),
        'enabled': bool(row['enabled']),
    }


class LlmProviderRepository:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def list_all(self) -> list[dict[str, Any]]:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f'SELECT {_COLUMNS} FROM llm_provider ORDER BY id')
            rows = await cur.fetchall()
        return [_row(row) for row in rows]

    async def get(self, provider_id: int) -> dict[str, Any] | None:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f'SELECT {_COLUMNS} FROM llm_provider WHERE id = %s', (provider_id,))
            row = await cur.fetchone()
        return _row(row) if row else None

    async def get_full(self, provider_id: int) -> dict[str, Any] | None:
        """Include the clear-text key. Only the LLM client may call this."""
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f'SELECT {_COLUMNS} FROM llm_provider WHERE id = %s', (provider_id,))
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_default(self) -> dict[str, Any] | None:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f'SELECT {_COLUMNS} FROM llm_provider WHERE is_default AND enabled LIMIT 1')
            row = await cur.fetchone()
        return dict(row) if row else None

    async def count(self) -> int:
        async with self._conn.cursor() as cur:
            await cur.execute('SELECT count(*) FROM llm_provider')
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def create(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        is_default: bool = False,
        enabled: bool = True,
    ) -> dict[str, Any]:
        now = utc_now_dt()
        # The first provider is the default whether or not the caller asked;
        # otherwise nothing would resolve.
        if not is_default:
            is_default = await self.count() == 0
        async with self._conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    f"""
                    INSERT INTO llm_provider
                        (name, base_url, api_key, model, is_default, enabled, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, FALSE, %s, %s, %s)
                    RETURNING {_COLUMNS}
                    """,
                    (name, base_url, api_key, model, enabled, now, now),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise ApiError('同名 provider 已存在', status=409) from exc
            row = await cur.fetchone()
        if is_default:
            await self.set_default(row['id'])
        return await self.get(row['id'])  # type: ignore[return-value]

    async def update(
        self,
        provider_id: int,
        changes: dict[str, Any],
        *,
        api_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Patch a provider. ``api_key=None`` leaves the stored key untouched."""
        sets: list[str] = []
        params: list[Any] = []
        for column in ('name', 'base_url', 'model', 'enabled'):
            if column in changes:
                sets.append(f'{column} = %s')
                params.append(changes[column])
        if api_key is not None:
            sets.append('api_key = %s')
            params.append(api_key)
        if not sets:
            return await self.get(provider_id)
        sets.append('updated_at = %s')
        params.extend([utc_now_dt(), provider_id])
        async with self._conn.cursor() as cur:
            try:
                await cur.execute(
                    f'UPDATE llm_provider SET {", ".join(sets)} WHERE id = %s',
                    params,
                )
            except psycopg.errors.UniqueViolation as exc:
                raise ApiError('同名 provider 已存在', status=409) from exc
        if changes.get('is_default'):
            await self.set_default(provider_id)
        return await self.get(provider_id)

    async def set_default(self, provider_id: int) -> None:
        """Make one provider the default, clearing any other in the same statement."""
        now = utc_now_dt()
        async with self._conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE llm_provider
                SET is_default = (id = %s), updated_at = %s
                WHERE is_default OR id = %s
                """,
                (provider_id, now, provider_id),
            )

    async def delete(self, provider_id: int) -> bool:
        async with self._conn.cursor() as cur:
            await cur.execute('DELETE FROM llm_provider WHERE id = %s', (provider_id,))
            return cur.rowcount > 0
