"""The fixed Caixin channel catalogue and its one-time migration."""

from __future__ import annotations

from ..storage import PostgresStorage
from .caixin import CHANNELS, SOURCE

CAIXIN_CATALOG_KEY = 'source:caixin:channel_catalog_v1'


async def ensure_caixin_channels(storage: PostgresStorage) -> int:
    """Create missing channels once without widening already-known collection.

    Before this release, a Caixin account was created only by subscribing. The
    migration therefore keeps channels with an active subscriber enabled and
    starts every other channel disabled. The marker makes the calculation
    one-time: later subscriptions belong to personal reading only.
    """
    async with storage.transaction(), storage.conn.cursor() as cur:
        await cur.execute("SELECT pg_advisory_xact_lock(hashtext('hippo.caixin.channel-catalog'))")
        await cur.execute('SELECT 1 FROM meta WHERE key = %s', (CAIXIN_CATALOG_KEY,))
        if await cur.fetchone():
            return 0
        for channel_id, (host, name) in CHANNELS.items():
            await cur.execute(
                """
                    INSERT INTO accounts
                        (biz, nickname, source, alias, is_disabled, backfill_state, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, TRUE, 'done', NOW(), NOW())
                    ON CONFLICT (biz) DO UPDATE SET
                        nickname = EXCLUDED.nickname,
                        source = EXCLUDED.source,
                        alias = EXCLUDED.alias,
                        updated_at = NOW()
                """,
                (f'{SOURCE}:{channel_id}', f'财新·{name}', SOURCE, host),
            )
        # Existing enabled rows represent subscription-created channels.
        # Explicitly disabled rows stay disabled.
        await cur.execute(
            """
                UPDATE accounts a
                   SET is_disabled = NOT EXISTS (
                       SELECT 1 FROM subscription s
                        WHERE s.biz = a.biz AND NOT s.is_disabled
                   ), updated_at = NOW()
                 WHERE a.source = %s AND NOT a.is_disabled
            """,
            (SOURCE,),
        )
        await cur.execute(
            'INSERT INTO meta(key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value',
            (CAIXIN_CATALOG_KEY, '1'),
        )
    return len(CHANNELS)


__all__ = ['CAIXIN_CATALOG_KEY', 'ensure_caixin_channels']
