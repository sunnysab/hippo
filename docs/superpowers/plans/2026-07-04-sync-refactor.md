# Sync Module Refactoring Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate duplicate code across sync_* files, break up the sync_service.py God Object, remove DRY violations, and modernize Python patterns.

**Architecture:** Merge duplicate State/Observer classes into shared base types, split sync_service.py into focused modules (settings, scheduler, service), extract repeated error handling into a reusable decorator, and simplify Null Object / serialization patterns.

**Tech Stack:** Python 3.14, FastAPI, psycopg3, Pydantic, dataclasses, asyncio

---

## File Structure (After Refactoring)

```
hippo/
  sync_types.py          # Unified data types (merge SyncJobState + SyncTaskState)
  sync_core.py           # Core sync loop (unchanged)
  sync_settings.py       # NEW: settings, status, history, alert (extracted from sync_service)
  sync_scheduler.py      # NEW: SyncScheduler (extracted from sync_service)
  sync_service.py         # ArticleSyncService + run_sync_job only (slimmed down)
  sync_jobs.py            # SyncJobRepository (unchanged)
  sync_tasks.py           # SyncTaskManager (slimmed, reuses shared types)
  sync_worker.py          # Worker (slimmed, reuses shared observer)
  config.py               # Simplified (inline env.py)
  exceptions.py           # Expanded exception hierarchy
  controllers/sync.py     # Slimmed (extracted error handling)
```

---

## Task 1: Merge SyncJobState and SyncTaskState into unified SyncTaskState

**Why:** These two dataclasses have nearly identical fields. One represents DB-persisted state, the other in-memory task tracking. We keep a single `SyncTaskState` that covers both use cases.

**Files:**
- Modify: `hippo/sync_types.py`
- Modify: `hippo/sync_jobs.py`
- Modify: `hippo/sync_tasks.py`
- Modify: `hippo/sync_worker.py`

- [ ] **Step 1: Add unified SyncTaskState to sync_types.py**

Add after the existing `SyncReport` class (before `SyncObserver`):

```python
@dataclass
class AccountProgress:
    biz: str
    nickname: str
    status: str = 'pending'
    phase: str | None = None
    saved: int = 0
    page_count: int = 0
    article_current: int | None = None
    article_total: int | None = None
    last_article: dict[str, Any] | None = None
    skip_reason: str | None = None
    error: str | None = None
    updated_at: str | None = None

    def touch(self) -> None:
        from .utils import utc_now_iso
        self.updated_at = utc_now_iso()


@dataclass
class SyncTaskState:
    task_id: str
    status: str = 'pending'
    created_at: str = ''
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    group_id: int | None = None
    biz_list: tuple[str, ...] | None = None
    trigger_type: str = 'manual'
    phase: str | None = None
    accounts_total: int = 0
    accounts_done: int = 0
    current_account: dict[str, Any] | None = None
    current_article: dict[str, Any] | None = None
    last_log: str | None = None
    report: dict[str, Any] | None = None
    accounts: dict[str, AccountProgress] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            'task_id': self.task_id,
            'status': self.status,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'error': self.error,
            'group_id': self.group_id,
            'phase': self.phase,
            'accounts_total': self.accounts_total,
            'accounts_done': self.accounts_done,
            'current_account': self.current_account,
            'current_article': self.current_article,
            'last_log': self.last_log,
            'report': self.report,
            'accounts': [p.__dict__.copy() for p in self.accounts.values()],
        }

    def to_summary_dict(self) -> dict[str, Any]:
        d = self.to_dict()
        d.pop('accounts', None)
        return d
```

- [ ] **Step 2: Update sync_jobs.py to use SyncTaskState**

Replace `SyncJobState` with `SyncTaskState` from sync_types. Change `_row_to_state` to construct a `SyncTaskState`:

```python
from .sync_types import AccountProgress, SyncTaskState

def _row_to_state(row: dict[str, Any] | None) -> SyncTaskState | None:
    if not row:
        return None
    accounts_raw = row.get('accounts') or []
    accounts: dict[str, AccountProgress] = {}
    for item in (accounts_raw if isinstance(accounts_raw, list) else []):
        if isinstance(item, dict):
            biz = item.get('biz', '')
            accounts[biz] = AccountProgress(
                biz=biz,
                nickname=item.get('nickname') or biz,
                status=item.get('status') or 'pending',
                phase=item.get('phase'),
                saved=item.get('saved') or 0,
                page_count=item.get('page_count') or 0,
                article_current=item.get('article_current'),
                article_total=item.get('article_total'),
                last_article=item.get('last_article'),
                skip_reason=item.get('skip_reason'),
                error=item.get('error'),
                updated_at=item.get('updated_at'),
            )
    biz_list_raw = row.get('biz_list')
    return SyncTaskState(
        task_id=str(row['id']),
        status=str(row['status']),
        created_at=str(normalize_value(row['created_at'])),
        started_at=normalize_value(row.get('started_at')),
        finished_at=normalize_value(row.get('finished_at')),
        error=row.get('error'),
        group_id=row.get('group_id'),
        biz_list=tuple(str(item) for item in biz_list_raw) if isinstance(biz_list_raw, list) else None,
        trigger_type=str(row.get('trigger_type') or 'manual'),
        phase=row.get('phase'),
        accounts_total=int(row.get('accounts_total') or 0),
        accounts_done=int(row.get('accounts_done') or 0),
        current_account=_normalize_dict(row.get('current_account')),
        current_article=_normalize_dict(row.get('current_article')),
        last_log=row.get('last_log'),
        report=_normalize_dict(row.get('report')),
        accounts=accounts,
    )
```

Update all `SyncJobRepository` methods to use `SyncTaskState` instead of `SyncJobState`. Update `__all__` to export `SyncTaskState` instead of `SyncJobState`.

- [ ] **Step 3: Remove SyncJobState and SyncTaskState from sync_tasks.py**

Delete the `SyncTaskState` and `AccountProgress` classes from `sync_tasks.py`. Import them from `sync_types` instead. Update `_SyncTaskJobObserver` and `SyncTaskManager` to use the shared types.

- [ ] **Step 4: Update sync_worker.py**

Replace `_WorkerProgressTracker` with a simpler adapter that writes `AccountProgress` objects from `sync_types` to the DB via `SyncJobRepository.update_progress`. The tracker should store `AccountProgress` dicts and serialize them on save.

- [ ] **Step 5: Run tests**

```bash
cd /home/sab/hippo && python -m pytest tests/ -x -q
```

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "refactor: unify SyncJobState and SyncTaskState into shared SyncTaskState"
```

---

## Task 2: Extract sync settings/status/history into sync_settings.py

**Why:** sync_service.py is 1116 lines. Settings management, status tracking, history, and alerting are independent of the sync execution logic.

**Files:**
- Create: `hippo/sync_settings.py`
- Modify: `hippo/sync_service.py`
- Modify: `hippo/sync_worker.py`
- Modify: `hippo/server.py`

- [ ] **Step 1: Create sync_settings.py**

Move the following from `sync_service.py` to `sync_settings.py`:
- Constants: `SYNC_STATUS_KEY`, `SYNC_ERROR_KEY`, `SYNC_STARTED_KEY`, `SYNC_FINISHED_KEY`, `SYNC_HISTORY_KEY`, `SYNC_SETTINGS_KEY`, `ALERT_SENT_KEY`, `SYNC_LOGIN_REQUIRED_AT_KEY`, `_ARTICLE_EXCLUDE_KEYWORD_LIMIT`
- Functions: `default_sync_settings`, `_split_article_exclude_keywords`, `_normalize_article_exclude_keywords`, `_normalize_window_start_hour`, `_normalize_window_end_hour`, `_get_window_hours`, `_is_within_sync_window`, `_seconds_until_window_start`, `get_sync_settings`, `set_sync_settings`, `append_sync_history`, `_send_sync_alert`, `get_sync_status`, `set_sync_state`, `_persist_sync_outcome`, `_parse_iso_datetime`, `_get_login_updated_at`, `_should_skip_for_login`, `_parse_date`, `_select_missing_content`, `_to_utc_timestamp`, `_today_str`

```python
"""Sync settings, status, and history management."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import DEFAULT_RECENT_DAYS, DEFAULT_WINDOW_END_HOUR, DEFAULT_WINDOW_START_HOUR
from .emailer import get_email_settings, send_email
from .storage import PostgresStorage, load_meta_json, save_meta_json
from .sync_core import is_freq_control, is_login_error
from .utils import parse_iso_date_to_timestamp, should_skip_by_time, utc_now_dt, utc_now_iso

# ... (move all the functions listed above here)
```

- [ ] **Step 2: Update sync_service.py imports**

Replace inline definitions with imports from `sync_settings`:

```python
from .sync_settings import (
    SYNC_LOGIN_REQUIRED_AT_KEY,
    _get_window_hours,
    _is_within_sync_window,
    _should_skip_for_login,
    _persist_sync_outcome,
    _parse_date,
    _select_missing_content,
    _to_utc_timestamp,
    _today_str,
    append_sync_history,
    default_sync_settings,
    get_sync_settings,
    get_sync_status,
    set_sync_settings,
    set_sync_state,
)
```

- [ ] **Step 3: Update sync_worker.py imports**

Change `from .sync_service import ...` to import settings functions from `sync_settings` where needed.

- [ ] **Step 4: Update server.py imports**

Change `from .sync_service import get_sync_settings as load_sync_settings` to `from .sync_settings import get_sync_settings as load_sync_settings`, etc.

- [ ] **Step 5: Run tests**

```bash
cd /home/sab/hippo && python -m pytest tests/ -x -q
```

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "refactor: extract sync settings/status/history into sync_settings.py"
```

---

## Task 3: Extract SyncScheduler into sync_scheduler.py

**Why:** The scheduler is an independent component that doesn't need to live in the service module.

**Files:**
- Create: `hippo/sync_scheduler.py`
- Modify: `hippo/sync_service.py`
- Modify: `hippo/server.py`

- [ ] **Step 1: Create sync_scheduler.py**

Move `SyncScheduler` class from `sync_service.py` to `sync_scheduler.py`. It imports from `sync_settings` and `sync_service`:

```python
"""Background sync scheduler."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime

from .storage import open_storage
from .sync_settings import _get_window_hours, _is_within_sync_window, get_sync_settings
from .sync_service import run_sync_job, SYNC_RUN_LOCK


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
```

Also move `_seconds_until_window_start` from sync_settings to sync_scheduler (it's only used by the scheduler).

- [ ] **Step 2: Remove SyncScheduler from sync_service.py**

Delete the `SyncScheduler` class and the `_seconds_until_window_start` function. Update `__all__`.

- [ ] **Step 3: Update server.py imports**

```python
from .sync_scheduler import SyncScheduler
```

- [ ] **Step 4: Run tests**

```bash
cd /home/sab/hippo && python -m pytest tests/ -x -q
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor: extract SyncScheduler into sync_scheduler.py"
```

---

## Task 4: Extract repeated error handling in controllers/sync.py

**Why:** The three sync functions (`sync_account_articles`, `sync_all_accounts`, `sync_group_accounts`) have identical except blocks repeated 3 times.

**Files:**
- Modify: `hippo/controllers/sync.py`

- [ ] **Step 1: Add a context manager for sync error handling**

Add at the top of `controllers/sync.py`:

```python
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

@asynccontextmanager
async def _sync_error_handler(
    storage: PostgresStorage,
    *,
    started_at: str,
) -> AsyncGenerator[None, None]:
    try:
        yield
    except SyncRunError as exc:
        finished_at = utc_now_iso()
        status = 'login_required' if exc.login_required else 'failed'
        _append_cli_sync_history(
            storage,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            saved=0,
            error=str(exc),
        )
        raise typer.Exit(code=1)
    except SyncInterrupted:
        finished_at = utc_now_iso()
        _append_cli_sync_history(
            storage,
            started_at=started_at,
            finished_at=finished_at,
            status='failed',
            saved=0,
            error='Interrupted',
        )
        raise typer.Exit(code=130)
```

- [ ] **Step 2: Refactor sync_account_articles**

```python
async def sync_account_articles(
    *,
    biz: str | None,
    pages: int,
    page_size: int,
    mode: SyncMode,
    recent_days: int | None,
    since_date: str | None,
    until_date: str | None,
    force: bool,
    skip_time: int | None,
    login_flow: Callable[..., Awaitable[None]] | None,
) -> None:
    config = _build_sync_config(
        mode=mode, page_size=page_size, sleep_seconds=0, reset=False,
        recent_days=recent_days, since_date=since_date, until_date=until_date,
        force=force, skip_minutes=skip_time,
    )
    _validate_cli_config(config)
    started_at = utc_now_iso()
    with open_storage() as storage:
        account = storage.accounts.get_account(biz)
        typer.echo(f'开始同步 {account.nickname} 的文章')
        async with _sync_error_handler(storage, started_at=started_at):
            report = await perform_sync(
                storage=storage, accounts=[account], config=config,
                bulk=False, login_flow=login_flow,
            )
    if report.summary:
        typer.echo(f'同步完成，共新增 {report.total_saved} 条记录')
```

- [ ] **Step 3: Apply same pattern to sync_all_accounts and sync_group_accounts**

Replace the try/except blocks in both functions with `async with _sync_error_handler(storage, started_at=started_at):`.

- [ ] **Step 4: Run tests**

```bash
cd /home/sab/hippo && python -m pytest tests/ -x -q
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor: extract repeated sync error handling into context manager"
```

---

## Task 5: Simplify Null Object pattern in sync_types.py

**Why:** All NullSyncObserver/NullSyncJobObserver methods just `return None`, which is the default. Use a cleaner approach.

**Files:**
- Modify: `hippo/sync_types.py`

- [ ] **Step 1: Replace NullSyncObserver with a minimal implementation**

```python
class NullSyncObserver:
    """Observer that silently discards all events."""

    def on_log(self, message: str) -> None: ...

    def on_progress(self, *, current: int | None, total: int | None, delta: int | None) -> None: ...

    def on_page(self, payload: dict[str, Any]) -> None: ...

    def on_complete(self, summary: SyncSummary) -> None: ...

    def on_skip(self, reason: str) -> None: ...
```

This is already the most Pythonic approach for Protocol-based null objects — the `...` (Ellipsis) body communicates "intentionally empty" better than `return None`. Keep `NullSyncJobObserver` the same way.

- [ ] **Step 2: Run tests**

```bash
cd /home/sab/hippo && python -m pytest tests/ -x -q
```

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "refactor: clean up Null Object implementations"
```

---

## Task 6: Simplify config.py and remove env.py

**Why:** `env.py` is a 9-line wrapper around `load_dotenv()`. `config.py` calls it at import time. The separation adds no value.

**Files:**
- Delete: `hippo/env.py`
- Modify: `hippo/config.py`
- Modify: `hippo/storage.py`

- [ ] **Step 1: Inline load_dotenv into config.py**

```python
"""Application configuration."""

from __future__ import annotations

import os
from typing import Final

from dotenv import load_dotenv

load_dotenv()

APP_NAME: Final = 'hippo'
# ... rest of constants unchanged
```

- [ ] **Step 2: Update all imports of `from .env import load_env`**

Search and replace:
- `from .env import load_env` → remove the import
- `load_env()` → remove the call (already called in config.py at import time)

Files to update: `storage.py` (line 17: `from .env import load_env`, line 224: `load_env()`)

- [ ] **Step 3: Delete env.py**

```bash
rm hippo/env.py
```

- [ ] **Step 4: Run tests**

```bash
cd /home/sab/hippo && python -m pytest tests/ -x -q
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor: inline env.py into config.py"
```

---

## Task 7: Fix global _cancel_event in sync_core.py

**Why:** Module-level global `_cancel_event` is shared across all callers. In multi-worker scenarios this causes cross-contamination. Pass it as a parameter instead.

**Files:**
- Modify: `hippo/sync_core.py`
- Modify: `hippo/sync_service.py`

- [ ] **Step 1: Refactor sync_core.py**

Replace the global `_cancel_event` with a function that creates a new event per call site. Add `cancel_event` as a parameter to `sync_account_core`:

```python
async def sync_account_core(
    *,
    storage: PostgresStorage,
    client: WeChatApiClient,
    account: AccountCredential,
    config: SyncConfig,
    plan: SyncPlan,
    cancel_event: asyncio.Event | None = None,
    login_flow: Callable[..., Awaitable[None]] | None = None,
    on_login_required: Callable[[], bool] | None = None,
    collect_existing_ids: bool = False,
    observer: SyncObserver | None = None,
) -> SyncSummary:
    cancel_event = cancel_event or asyncio.Event()
    # ... rest of function, replacing _get_cancel_event() with cancel_event
```

Remove `_cancel_event`, `_get_cancel_event`, `request_sync_cancel`, `reset_sync_cancel` from sync_core.py. Instead, the cancel mechanism moves to sync_service.py where it's actually managed:

```python
# sync_service.py
_sync_cancel_event: asyncio.Event | None = None

def request_sync_cancel() -> None:
    global _sync_cancel_event
    if _sync_cancel_event:
        _sync_cancel_event.set()

def reset_sync_cancel() -> None:
    global _sync_cancel_event
    _sync_cancel_event = asyncio.Event()
```

- [ ] **Step 2: Update callers**

In `sync_service.py`, pass `cancel_event=_sync_cancel_event` to `sync_account_core`.

- [ ] **Step 3: Run tests**

```bash
cd /home/sab/hippo && python -m pytest tests/ -x -q
```

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "refactor: remove global _cancel_event from sync_core, pass as parameter"
```

---

## Task 8: Simplify dataclass serialization

**Why:** `SyncTaskState.to_dict()` manually lists every field. Use `dataclasses.asdict` where possible, and add `to_dict` to `SyncReport`.

**Files:**
- Modify: `hippo/sync_types.py`

- [ ] **Step 1: Simplify SyncTaskState.to_dict()**

```python
from dataclasses import asdict, field

class SyncTaskState:
    # ... fields ...

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        # Convert AccountProgress instances to plain dicts
        result['accounts'] = [p.__dict__.copy() for p in self.accounts.values()]
        return result

    def to_summary_dict(self) -> dict[str, Any]:
        d = self.to_dict()
        d.pop('accounts', None)
        return d
```

- [ ] **Step 2: Add to_dict to SyncReport**

```python
@dataclass(frozen=True)
class SyncReport:
    total_saved: int
    summary: list[tuple[str, int]]
    details: list[SyncAccountResult]
    downloaded: int = 0
    failed_accounts: int = 0
    accounts_total: int = 0
    accounts_done: int = 0
    current_account: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)
```

- [ ] **Step 3: Replace manual report serialization in sync_worker.py**

Replace `_normalize_report` with `result.report.to_dict()`.

- [ ] **Step 4: Run tests**

```bash
cd /home/sab/hippo && python -m pytest tests/ -x -q
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor: simplify dataclass serialization with asdict"
```

---

## Task 9: Expand exception hierarchy

**Why:** `exceptions.py` only has `ApiError`. Add domain-specific exceptions for better error handling.

**Files:**
- Modify: `hippo/exceptions.py`
- Modify: `hippo/sync_core.py`
- Modify: `hippo/sync_service.py`

- [ ] **Step 1: Expand exceptions.py**

```python
"""Application exceptions."""

from __future__ import annotations


class HippoError(RuntimeError):
    """Base exception for all Hippo errors."""


class ApiError(HippoError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class SyncError(HippoError):
    """Base exception for sync-related errors."""


class SyncInterrupted(SyncError):
    """Raised when sync is cancelled by user."""


class SyncLoginRequired(SyncError):
    """Raised when WeChat login session has expired."""


class SyncFreqControlled(SyncError):
    """Raised when WeChat frequency control is triggered."""


class StorageError(HippoError):
    """Raised when database operations fail."""


class StorageInitError(StorageError):
    """Raised when database is not initialized or schema is outdated."""
```

- [ ] **Step 2: Update sync_core.py**

Replace `SyncInterrupted(RuntimeError)` with `from .exceptions import SyncInterrupted`.

- [ ] **Step 3: Update sync_service.py**

Replace `SyncRunError(RuntimeError)` with imports from exceptions. Update error handling to use the new hierarchy.

- [ ] **Step 4: Update storage.py**

Replace `StorageInitError(RuntimeError)` with `from .exceptions import StorageInitError`.

- [ ] **Step 5: Run tests**

```bash
cd /home/sab/hippo && python -m pytest tests/ -x -q
```

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "refactor: expand exception hierarchy with domain-specific errors"
```

---

## Task 10: Fix async fallback in sync_service.py

**Why:** `sync_service.py:988-991` has a broken async fallback pattern.

**Files:**
- Modify: `hippo/sync_service.py`

- [ ] **Step 1: Fix the asyncio.create_task fallback**

Replace:
```python
try:
    asyncio.create_task(_run_backfill_images())
except RuntimeError:
    loop = asyncio.get_event_loop()
    loop.create_task(_run_backfill_images())
```

With:
```python
asyncio.create_task(_run_backfill_images())
```

If `create_task` fails because there's no running loop, the caller is already in an async context where this shouldn't happen. The fallback was incorrect anyway.

- [ ] **Step 2: Run tests**

```bash
cd /home/sab/hippo && python -m pytest tests/ -x -q
```

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "fix: remove broken async fallback for image backfill task"
```

---

## Task 11: Final cleanup and verification

- [ ] **Step 1: Run full test suite**

```bash
cd /home/sab/hippo && python -m pytest tests/ -v
```

- [ ] **Step 2: Run ruff linter**

```bash
cd /home/sab/hippo && ruff check hippo/
```

- [ ] **Step 3: Run ruff formatter**

```bash
cd /home/sab/hippo && ruff format hippo/
```

- [ ] **Step 4: Verify imports are clean**

```bash
cd /home/sab/hippo && python -c "import hippo"
```

- [ ] **Step 5: Commit final state**

```bash
git add -A && git commit -m "refactor: sync module cleanup — final pass"
```

---

## Summary of Changes

| Task | What | Lines Removed (est.) |
|------|------|---------------------|
| 1 | Merge SyncJobState/SyncTaskState | ~150 |
| 2 | Extract sync_settings.py | 0 (moved) |
| 3 | Extract sync_scheduler.py | 0 (moved) |
| 4 | Extract error handling ctx mgr | ~60 |
| 5 | Clean up Null Objects | ~30 |
| 6 | Inline env.py | ~15 |
| 7 | Fix global cancel_event | ~20 |
| 8 | Simplify serialization | ~40 |
| 9 | Expand exceptions | ~10 |
| 10 | Fix async fallback | ~5 |
| **Total** | | **~330 lines removed** |

**Net result:** sync_service.py goes from 1116 lines to ~600 lines. ~330 lines of duplicate/boilerplate code eliminated. Clear module boundaries. Consistent patterns.
