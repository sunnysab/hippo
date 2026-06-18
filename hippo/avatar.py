"""Avatar image caching and retrieval."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import psycopg

from .article_queries import _normalize_record
from .storage import PostgresStorage, fetchone_row
from .utils import utc_now_iso

logger = logging.getLogger('hippo.serve')


def _ensure_avatar_images_table(storage: PostgresStorage) -> None:
    with storage.transaction(), storage.conn.cursor() as cur:
        cur.execute(
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


def _get_avatar_row(storage: PostgresStorage, biz: str) -> dict[str, Any] | None:
    return fetchone_row(
        storage,
        'SELECT avatar_url, content_type, data FROM avatar_images WHERE biz = %s',
        [biz],
        normalize=_normalize_record,
    )


def _upsert_avatar_url(storage: PostgresStorage, biz: str, url: str) -> None:
    with storage.transaction(), storage.conn.cursor() as cur:
        cur.execute(
            """
                INSERT INTO avatar_images (biz, avatar_url, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (biz) DO UPDATE SET
                    avatar_url=EXCLUDED.avatar_url,
                    updated_at=EXCLUDED.updated_at
                """,
            (biz, url, utc_now_iso()),
        )


def _store_avatar(
    storage: PostgresStorage,
    biz: str,
    *,
    content_type: str,
    data: bytes,
    avatar_url: str | None = None,
) -> None:
    with storage.transaction(), storage.conn.cursor() as cur:
        cur.execute(
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


def _fetch_and_cache_avatar(storage: PostgresStorage, biz: str, url: str) -> tuple[bytes, str] | None:
    headers = {
        'Referer': 'https://mp.weixin.qq.com/',
        'Origin': 'https://mp.weixin.qq.com',
        'User-Agent': 'Mozilla/5.0',
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None
        content_type = resp.headers.get('Content-Type') or 'application/octet-stream'
        data = resp.content
        if data:
            _store_avatar(storage, biz, content_type=content_type, data=data, avatar_url=url)
        return data, content_type
    except Exception as exc:
        logger.warning('Failed to fetch avatar for %s: %s', biz, exc)
        return None
