"""Avatar image caching and retrieval."""

from __future__ import annotations

from typing import Any

import psycopg

from .article_queries import _normalize_record
from .http import MPClient
from .logger import get_logger
from .public_image import fetch_public_image
from .storage import PostgresStorage, fetchone_row
from .utils import utc_now_iso

logger = get_logger(__name__)


async def _ensure_avatar_images_table(storage: PostgresStorage) -> None:
    async with storage.transaction(), storage.conn.cursor() as cur:
        await cur.execute(
            """
                CREATE TABLE IF NOT EXISTS avatar_images (
                    biz TEXT PRIMARY KEY,
                    avatar_url TEXT,
                    content_type TEXT,
                    data BYTEA,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
        )


async def _get_avatar_row(storage: PostgresStorage, biz: str) -> dict[str, Any] | None:
    return await fetchone_row(
        storage,
        'SELECT avatar_url, content_type, data FROM avatar_images WHERE biz = %s',
        [biz],
        normalize=_normalize_record,
    )


async def _upsert_avatar_url(storage: PostgresStorage, biz: str, url: str) -> None:
    async with storage.transaction(), storage.conn.cursor() as cur:
        await cur.execute(
            """
                INSERT INTO avatar_images (biz, avatar_url, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (biz) DO UPDATE SET
                    avatar_url=EXCLUDED.avatar_url,
                    updated_at=EXCLUDED.updated_at
                """,
            (biz, url, utc_now_iso()),
        )


async def _store_avatar(
    storage: PostgresStorage,
    biz: str,
    *,
    content_type: str,
    data: bytes,
    avatar_url: str | None = None,
) -> None:
    async with storage.transaction(), storage.conn.cursor() as cur:
        await cur.execute(
            """
                INSERT INTO avatar_images (biz, avatar_url, content_type, data, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (biz) DO UPDATE SET
                    avatar_url=COALESCE(EXCLUDED.avatar_url, avatar_images.avatar_url),
                    content_type=EXCLUDED.content_type,
                    data=EXCLUDED.data,
                    updated_at=EXCLUDED.updated_at
                """,
            (biz, avatar_url, content_type, psycopg.Binary(data), utc_now_iso()),
        )


async def _avatar_proxy(storage: PostgresStorage, biz: str) -> str:
    """The exit this avatar leaves through: the one its account's source uses.

    A YouTube account's avatar sits on a host the box cannot reach directly, so
    a fixed WeChat proxy here leaves that avatar unfetched. An unknown biz (the
    search path caches under ``gh_id``, which is not an ``accounts.biz``) stays
    on WeChat traffic.
    """
    from .media_settings import get_media_settings, resolve_proxy

    row = await fetchone_row(
        storage,
        'SELECT source FROM accounts WHERE biz = %s',
        [biz],
        normalize=_normalize_record,
    )
    source = str((row or {}).get('source') or '') or 'weixin'
    return resolve_proxy(await get_media_settings(storage), source)


async def _fetch_and_cache_avatar(storage: PostgresStorage, biz: str, url: str) -> tuple[bytes, str] | None:
    headers = {
        'Referer': 'https://mp.weixin.qq.com/',
        'Origin': 'https://mp.weixin.qq.com',
        'User-Agent': 'Mozilla/5.0',
    }
    proxy = await _avatar_proxy(storage, biz)
    try:
        # The avatar URL is untrusted (feed or channel metadata), so the fetch is
        # pinned to a checked public IP per hop; the source's proxy is kept. The
        # avatar profile also certifies each original hostname and keeps no
        # keep-alive, so a pooled channel cannot serve a different host.
        async with MPClient(timeout=10.0, proxy=proxy or None, pinned_proxy_sni=True) as client:
            fetched = await fetch_public_image(client.client, url, headers=headers)
        if fetched is None:
            return None
        data, content_type = fetched
        if data:
            await _store_avatar(storage, biz, content_type=content_type, data=data, avatar_url=url)
        return data, content_type
    except Exception as exc:
        logger.warning('Failed to fetch avatar for %s: %s', biz, exc)
        return None
