"""Report scheduler.

Same shape as :class:`~hippo.sync_scheduler.SyncScheduler`: an ``asyncio.Event``
loop that wakes on a timer or on an explicit trigger. It ticks hourly rather
than per user, because each user's ``send_hour`` is in their own time zone and
the delivery ledger already guarantees at most one send per local day.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from .logger import get_logger
from .report.delivery import deliver_due
from .storage import open_storage

logger = get_logger(__name__)

#: How long to sleep between hourly checks.
TICK_SECONDS = 60 * 60


class ReportScheduler:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._trigger = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._trigger.clear()
        self._loop = asyncio.get_running_loop()
        self._task = self._loop.create_task(self._loop_run())

    async def stop(self) -> None:
        self._stop.set()
        self._trigger.set()
        if self._task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    def trigger(self) -> None:
        """Wake the loop now — used by the manual send button and by tests."""
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._trigger.set)
        else:
            self._trigger.set()

    async def _wait(self, timeout: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._trigger.wait(), timeout=timeout)
        self._trigger.clear()

    async def _loop_run(self) -> None:
        while not self._stop.is_set():
            # Wait first: a fresh start should not fire a report immediately,
            # because the ledger already covered the current day.
            await self._wait(TICK_SECONDS)
            if self._stop.is_set():
                break
            try:
                await self.run_once()
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception('Report scheduler tick failed: %s', exc)

    async def run_once(self) -> dict[str, Any]:
        """Deliver everything due in the current hour, once."""
        if self._lock.locked():
            return {'status': 'running'}
        async with self._lock:
            hour = datetime.now(UTC).hour
            async with open_storage() as storage:
                sent = await deliver_due(storage, hour)
            if sent:
                logger.info('Report scheduler delivered %d report(s)', sent)
            return {'status': 'ok', 'sent': sent}


__all__ = ['TICK_SECONDS', 'ReportScheduler']
