"""A small web-owned loop: leases survive disconnects and recover after restarts."""

from __future__ import annotations

import asyncio

from .exceptions import ApiError
from .llm.client import resolve_provider
from .logger import get_logger
from .outline import generate_outline, snapshot
from .storage import open_storage

logger = get_logger(__name__)
JOB_TIMEOUT = 600  # Shorter than the 15-minute database lease, including reduction calls.


async def run_once() -> bool:
    async with open_storage() as storage, storage.transaction():
        claim = await storage.outlines.claim()
    if claim is None:
        return False
    try:
        async with asyncio.timeout(JOB_TIMEOUT):
            async with open_storage() as storage:
                row = await storage.outlines.source(claim['article_pk'])
                provider = await resolve_provider(storage)
            source = snapshot(row['title'], row['content_json']) if row else None
            if source is None or source.fingerprint != claim['fingerprint']:
                raise ApiError('The article content changed.', code='outline.content_changed')
            sections = await generate_outline(provider, source)
            async with open_storage() as storage, storage.transaction():
                current = await storage.outlines.source(claim['article_pk'])
                if (
                    current is None
                    or snapshot(current['title'], current['content_json']).fingerprint != source.fingerprint
                ):
                    raise ApiError('The article content changed.', code='outline.content_changed')
                await storage.outlines.finish(claim, sections=sections, model=provider['model'])
    except asyncio.CancelledError:
        async with open_storage() as storage, storage.transaction():
            await storage.outlines.release(claim)
        raise
    except Exception as exc:
        code = (
            exc.code if isinstance(exc, ApiError) and exc.code.startswith('outline.') else 'outline.generation_failed'
        )
        logger.warning('outline_generation_failed', article_id=claim['article_pk'], code=code)
        async with open_storage() as storage, storage.transaction():
            await storage.outlines.finish(claim, error=code)
    return True


async def run_loop() -> None:
    while True:
        try:
            worked = await run_once()
        except Exception:
            logger.warning('outline_queue_unavailable')
            worked = False
        if not worked:
            await asyncio.sleep(3)
