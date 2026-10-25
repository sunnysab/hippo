"""A web-owned classification loop: backfill, then one article per tick.

Same shape as the outline worker, with one difference worth naming: the queue
backfills itself here. Nothing outside enqueues a classification, so the loop
first adds the articles it has never seen, then claims one. Per-article writes
are short and never wrap the model call.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from .exceptions import ApiError
from .llm.client import resolve_provider
from .logger import get_logger
from .site_settings import get_site_settings
from .storage import open_storage
from .topics import classify, fingerprint

logger = get_logger(__name__)

#: Shorter than the 15-minute database lease, so a hung call cannot expire the
#: lease of the run that is still working.
JOB_TIMEOUT = 120

#: How far back the backfill reaches. Whole-library classification would mean
#: tens of thousands of model calls, so the window is deliberately not a knob.
BACKFILL_DAYS = 30

#: Idle pause between ticks. The loop claims one article at a time and shares the
#: LLM gate with user conversations, so it must not spin.
IDLE_SECONDS = 3

#: One requeue batch per tick; a vocabulary change can leave every row behind.
REQUEUE_BATCH = 500


async def _vocabulary(storage) -> dict[str, Any]:
    return (await get_site_settings(storage))['vocabulary']


async def _backfill(storage, vocabulary: dict[str, Any]) -> None:
    since = int((datetime.now(UTC) - timedelta(days=BACKFILL_DAYS)).timestamp())
    await storage.topics.backfill(since_ts=since, vocabulary_version=int(vocabulary['version']))
    # Only the rows behind the current vocabulary: this is the switch that makes
    # an edited topic list take effect, and it must not block normal work.
    try:
        await storage.topics.requeue_stale(vocabulary_version=int(vocabulary['version']), limit=REQUEUE_BATCH)
    except Exception:
        logger.warning('topic_requeue_unavailable')


async def run_once() -> bool:
    async with open_storage() as storage, storage.transaction():
        vocabulary = await _vocabulary(storage)
        await _backfill(storage, vocabulary)
        claim = await storage.topics.claim()
    if claim is None:
        return False

    try:
        async with asyncio.timeout(JOB_TIMEOUT):
            async with open_storage() as storage:
                row = await storage.topics.source(claim['article_pk'])
            if row is None:
                raise ApiError('The article is gone.', code='topic.content_changed')
            current = fingerprint(row['title'], row['digest'])
            # The queue row was written before the title was ever read, so this is
            # where an unchanged article is recognised and skipped for free.
            if claim['fingerprint'] == current and claim['state'] == 'processing' and claim['topics']:
                # Keep whatever event the article already had: skipping the model
                # is not a statement that the story disappeared.
                async with open_storage() as storage, storage.transaction():
                    await storage.topics.finish(
                        claim,
                        topics=claim['topics'],
                        fingerprint=current,
                        vocabulary_version=int(vocabulary['version']),
                        keep_event=True,
                    )
                return True

            async with open_storage() as storage:
                provider = await resolve_provider(storage)
                candidates = await storage.topics.candidates()
            topics, event = await classify(
                provider,
                title=row['title'],
                digest=row['digest'],
                vocabulary=vocabulary,
                candidates=candidates,
            )

            async with open_storage() as storage, storage.transaction():
                latest = await storage.topics.source(claim['article_pk'])
                if latest is None or fingerprint(latest['title'], latest['digest']) != current:
                    raise ApiError('The article changed.', code='topic.content_changed')
                await storage.topics.finish(
                    claim,
                    topics=topics,
                    fingerprint=current,
                    vocabulary_version=int(vocabulary['version']),
                    model=provider['model'],
                    event=event,
                )
    except asyncio.CancelledError:
        async with open_storage() as storage, storage.transaction():
            await storage.topics.release(claim)
        raise
    except Exception as exc:
        code = exc.code if isinstance(exc, ApiError) and exc.code.startswith('topic.') else 'topic.generation_failed'
        logger.warning('topic_classification_failed', article_id=claim['article_pk'], code=code)
        async with open_storage() as storage, storage.transaction():
            await storage.topics.finish(claim, error=code)
    return True


async def run_loop() -> None:
    while True:
        try:
            worked = await run_once()
        except Exception:
            logger.warning('topic_queue_unavailable')
            worked = False
        if not worked:
            await asyncio.sleep(IDLE_SECONDS)


__all__ = ['BACKFILL_DAYS', 'IDLE_SECONDS', 'JOB_TIMEOUT', 'REQUEUE_BATCH', 'run_loop', 'run_once']
