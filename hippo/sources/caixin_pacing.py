"""A durable access budget shared by every Caixin caller and process."""

from __future__ import annotations

import json
import math
import random
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.rows import dict_row

from ..logger import get_logger
from . import SourceDeferred, SourceError

STATE_KEY = 'source:caixin:pacing'
DAILY_OPERATIONS = 60
DAILY_ARTICLES = 20
LEASE_SECONDS = 120
ERROR_COOLDOWN_SECONDS = 900

logger = get_logger(__name__)


class CaixinPaused(SourceDeferred):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, math.ceil(retry_after_seconds))
        super().__init__(f'财新暂时休息，请在 {self.retry_after_seconds} 秒后重试')


def _reserve(state: dict[str, Any], kind: str, now: float) -> str:
    today = datetime.fromtimestamp(now, UTC)
    day = today.date().isoformat()
    if state.get('day') != day:
        # A date change resets budgets, but never an ongoing request or rest.
        state.update(day=day, daily_ops=0, daily_articles=0)
    eligible_at = max(state.get('next_at', 0), state.get('lease_until', 0))
    if eligible_at > now:
        raise CaixinPaused(math.ceil(eligible_at - now))
    if state.get('daily_ops', 0) >= DAILY_OPERATIONS or (
        kind == 'article' and state.get('daily_articles', 0) >= DAILY_ARTICLES
    ):
        tomorrow = datetime.combine(today.date() + timedelta(days=1), datetime.min.time(), UTC)
        raise CaixinPaused(math.ceil(tomorrow.timestamp() - now))

    delay = random.randint(15, 30) if kind == 'list' else random.randint(30, 90)
    if kind == 'article':
        remaining = state.get('remaining', random.randint(3, 5)) - 1
        state['daily_articles'] = state.get('daily_articles', 0) + 1
        if remaining <= 0:
            delay = random.randint(1800, 5400)
            remaining = random.randint(3, 5)
        state['remaining'] = remaining
    token = uuid.uuid4().hex
    state.update(
        daily_ops=state.get('daily_ops', 0) + 1,
        next_at=now + delay,
        lease_token=token,
        lease_until=now + LEASE_SECONDS,
        lease_delay=delay,
    )
    return token


def _release(state: dict[str, Any], token: str, now: float, error: BaseException | None) -> None:
    if state.get('lease_token') != token:
        return
    delay = state.pop('lease_delay', 0)
    if error is not None:
        retry_after = getattr(error, 'retry_after_seconds', 0)
        if not isinstance(retry_after, (int, float)) or not math.isfinite(retry_after):
            retry_after = 0
        delay = max(delay, ERROR_COOLDOWN_SECONDS, retry_after)
    state.update(next_at=max(state.get('next_at', 0), now + delay), lease_token=None, lease_until=0)


def _open_storage():
    from ..storage import open_storage

    return open_storage()


@asynccontextmanager
async def _locked_state():
    async with _open_storage() as storage, storage.transaction():
        await storage.conn.execute(
            'INSERT INTO meta(key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING',
            (STATE_KEY, '{}'),
        )
        async with storage.conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute('SELECT value FROM meta WHERE key = %s FOR UPDATE', (STATE_KEY,))
            row = await cursor.fetchone()
        try:
            state = json.loads(row['value'])
            if not isinstance(state, dict):
                raise ValueError
        except TypeError, ValueError:
            raise SourceError('财新访问节奏记录无效，请检查本地服务状态') from None
        yield state
        await storage.meta.set(STATE_KEY, json.dumps(state))


class CaixinPacer:
    """Reserve before network I/O; release without sleeping or retaining a connection."""

    @asynccontextmanager
    async def request(self, kind: str):
        if kind not in {'list', 'article'}:
            raise ValueError('Unknown Caixin request kind')
        async with _locked_state() as state:
            token = _reserve(state, kind, time.time())
        error: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                async with _locked_state() as state:
                    _release(state, token, time.time(), error)
            except Exception as exc:
                # The persisted lease expires conservatively if cleanup cannot reach PG.
                logger.warning('财新访问节奏状态释放失败：%s', type(exc).__name__)
