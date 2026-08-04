"""Probe WeRead article-list rate-limit recovery without syncing data."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime

from hippo.http import MPClient
from hippo.storage import open_storage
from hippo.sync_core import is_freq_control
from hippo.wechat_api import WeChatApiClient


async def probe_until_available(
    api: WeChatApiClient,
    session: object,
    *,
    biz: str,
    interval_seconds: float,
    max_attempts: int,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    for attempt in range(1, max_attempts + 1):
        timestamp = datetime.now().astimezone().isoformat(timespec='seconds')
        try:
            payload = await api.list_articles(session, biz=biz, offset=0, count=1)
        except RuntimeError as exc:
            if not is_freq_control(str(exc)):
                print(f'{timestamp} error attempt={attempt}: {exc}')
                return 1
            print(f'{timestamp} blocked attempt={attempt}: {exc}')
        except Exception as exc:
            print(f'{timestamp} error attempt={attempt}: {exc}')
            return 1
        else:
            print(f'{timestamp} recovered attempt={attempt} items={len(payload.get("data") or [])}')
            return 0

        if attempt < max_attempts:
            await sleep(interval_seconds)

    return 2


def positive_number(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('must be greater than zero')
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Probe WeRead article-list rate-limit recovery.')
    parser.add_argument('--biz', required=True, help='Account fakeid or WeRead book ID to probe')
    parser.add_argument('--interval-seconds', type=positive_number, default=900.0, help='Seconds between probes')
    parser.add_argument('--max-attempts', type=int, default=8, help='Maximum probe requests')
    parser.add_argument('--timeout-seconds', type=positive_number, default=20.0, help='HTTP timeout per probe')
    args = parser.parse_args()
    if args.max_attempts <= 0:
        parser.error('--max-attempts must be greater than zero')
    return args


async def run() -> int:
    args = parse_args()
    with open_storage() as storage:
        session = storage.sessions.get_login_session()
    async with MPClient(timeout=args.timeout_seconds) as client:
        return await probe_until_available(
            WeChatApiClient(client),
            session,
            biz=args.biz,
            interval_seconds=args.interval_seconds,
            max_attempts=args.max_attempts,
        )


if __name__ == '__main__':
    raise SystemExit(asyncio.run(run()))
