"""Optional in-process runner for the same persistent worker used by systemd.

There is one execution path: all discovery jobs live in sync_jobs. Embedded
mode must not bypass that queue or omit the body/push consumers.
"""

from __future__ import annotations

import asyncio
import contextlib


class SyncWorkerRunner:
    """Own the optional embedded worker lifecycle."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        from .sync_worker import run_sync_worker

        self._task = asyncio.create_task(run_sync_worker(configure_process=False))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
