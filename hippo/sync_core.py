"""同步任务的中断开关。

这里原先还有微信读书的分页 core（``sync_account_core`` / ``_fetch_with_retry`` /
``parse_mp_chapters``）。列表与正文数据源换成 weixin-rs daemon 之后那些都没有调用方，
只剩下取消机制给 ``sync_service`` 用。
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar

_cancel_event: ContextVar[asyncio.Event | None] = ContextVar('sync_cancel', default=None)


def _get_cancel_event() -> asyncio.Event:
    event = _cancel_event.get()
    if event is None:
        event = asyncio.Event()
        _cancel_event.set(event)
    return event


def request_sync_cancel() -> None:
    _get_cancel_event().set()


def reset_sync_cancel() -> None:
    _cancel_event.set(asyncio.Event())


__all__ = ['request_sync_cancel', 'reset_sync_cancel']
