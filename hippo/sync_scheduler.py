"""Background sync scheduler."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from typing import Any

from .storage import open_storage
from .sync_service import SYNC_RUN_LOCK, run_sync_job
from .sync_settings import _get_window_hours, _is_within_sync_window, _seconds_until_window_start, get_sync_settings


class SyncScheduler:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._trigger = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._trigger.clear()
        self._loop = asyncio.get_running_loop()
        self._task = self._loop.create_task(self._loop_sync())

    async def stop(self) -> None:
        self._stop.set()
        self._trigger.set()
        if self._task:
            await self._task
        self._task = None

    def trigger(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._trigger.set)
        else:
            self._trigger.set()

    async def _wait(self, timeout: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._trigger.wait(), timeout=timeout)
        self._trigger.clear()

    async def _loop_sync(self) -> None:
        last_run_duration = 0.0
        while not self._stop.is_set():
            with open_storage() as storage:
                settings = get_sync_settings(storage)
            if not settings.get('enabled'):
                last_run_duration = 0.0
                await self._wait(10)
                continue
            start_hour, end_hour = _get_window_hours(settings)
            now = datetime.now()
            if not _is_within_sync_window(now, start_hour=start_hour, end_hour=end_hour):
                last_run_duration = 0.0
                await self._wait(_seconds_until_window_start(now, start_hour=start_hour, end_hour=end_hour))
                continue
            interval = max(int(settings.get('interval_minutes') or 1), 1) * 60
            wait_seconds = max(interval - last_run_duration, 0)
            await self._wait(wait_seconds)
            if self._stop.is_set():
                break
            now = datetime.now()
            if not _is_within_sync_window(now, start_hour=start_hour, end_hour=end_hour):
                last_run_duration = 0.0
                continue
            loop = self._loop or asyncio.get_running_loop()
            started_at = loop.time()
            await self.run_once()
            last_run_duration = max(loop.time() - started_at, 0.0)

    async def run_once(self, *, group_id: int | None = None) -> dict[str, Any]:
        if self._lock.locked() or SYNC_RUN_LOCK.locked():
            return {'status': 'running'}
        async with self._lock:
            return await self._run_sync_async(group_id=group_id)

    async def run_group(self, group_id: int) -> dict[str, Any]:
        return await self.run_once(group_id=group_id)

    async def _run_sync_async(self, *, group_id: int | None = None) -> dict[str, Any]:
        result = await run_sync_job(group_id=group_id, lock=SYNC_RUN_LOCK)
        return result.status


__all__ = ['SyncScheduler']
