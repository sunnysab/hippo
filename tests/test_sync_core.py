from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import patch

from hippo.models import AccountCredential, LoginSession
from hippo.sync_core import sync_account_core
from hippo.sync_types import NullSyncObserver, SyncConfig, SyncMode, SyncPlan


class _Meta:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def delete(self, key: str) -> None:
        self.store.pop(key, None)


class _Articles:
    def __init__(self) -> None:
        self.saved: list[Any] = []

    def get_existing_article_ids(self, biz: str, ids: list[str]) -> set[str]:
        return set()

    def save_articles(self, records: list[Any]) -> int:
        self.saved.extend(records)
        return len(records)

    def update_last_synced(self, biz: str) -> None:
        return None


class _Sessions:
    def get_login_session(self) -> LoginSession:
        return LoginSession(vid='1', access_token='tok')


class _Accounts:
    def update_last_synced(self, biz: str) -> None:
        return None


class _Tx:
    def __enter__(self) -> _Tx:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class _FakeStorage:
    def __init__(self) -> None:
        self.meta = _Meta()
        self.articles = _Articles()
        self.sessions = _Sessions()
        self.accounts = _Accounts()

    def __enter__(self) -> _FakeStorage:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def transaction(self) -> _Tx:
        return _Tx()

    def rollback(self) -> None:
        return None


class _FakeClient:
    def __init__(self, page_size: int) -> None:
        self._page_size = page_size
        self.calls: list[int] = []

    async def list_articles(self, session: Any, *, biz: str, offset: int, count: int) -> dict[str, Any]:
        self.calls.append(offset)
        # Always return a full page so only max_pages stops the loop.
        return {
            'data': [
                {
                    'reviewId': f'MP_WXS_3075193430_{offset}_{i}',
                    'createTime': 1700000000 + offset + i,
                    'mpInfo': {'title': f'Article {offset}-{i}', 'pic_url': ''},
                }
                for i in range(self._page_size)
            ]
        }


def _make_account() -> AccountCredential:
    return AccountCredential(biz='MzA3NTE5MzQzMA==', nickname='test')


def _make_plan() -> SyncPlan:
    return SyncPlan(
        since_timestamp=None,
        until_timestamp=None,
        stop_on_existing=False,
        full_synced_hint=False,
        resume_key=None,
        complete_key=None,
    )


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


class SyncCoreMaxPagesTest(unittest.TestCase):
    def _sync(self, max_pages: int | None, page_size: int = 5) -> tuple[_FakeClient, _FakeStorage, Any]:
        client = _FakeClient(page_size)
        storage = _FakeStorage()
        config = SyncConfig(
            mode=SyncMode.full,
            page_size=page_size,
            sleep_seconds=0,
            reset=False,
            recent_days=None,
            since_date=None,
            until_date=None,
            force=False,
            skip_minutes=None,
            download_content=False,
            download_images=False,
            content_limit=0,
            max_pages=max_pages,
        )
        with patch('hippo.sync_core.open_storage', return_value=storage):
            summary = _run(
                sync_account_core(
                    storage=storage,
                    client=client,
                    account=_make_account(),
                    config=config,
                    plan=_make_plan(),
                    observer=NullSyncObserver(),
                )
            )
        return client, storage, summary

    def test_max_pages_stops_after_requested_pages(self) -> None:
        client, storage, summary = self._sync(max_pages=2, page_size=5)
        self.assertEqual(client.calls, [0, 5])
        self.assertEqual(len(storage.articles.saved), 10)
        self.assertTrue(summary.completed)

    def test_max_pages_one_fetches_single_page(self) -> None:
        client, storage, summary = self._sync(max_pages=1, page_size=5)
        self.assertEqual(client.calls, [0])
        self.assertEqual(len(storage.articles.saved), 5)
        self.assertTrue(summary.completed)

    def test_no_max_pages_keeps_going_until_short_page(self) -> None:
        # With no max_pages, the loop stops only on a short/empty page. Use a
        # client that returns a short page on the 3rd fetch to bound the test.
        client = _FakeClient(page_size=5)

        async def list_articles(session, *, biz, offset, count):
            client.calls.append(offset)
            size = 2 if offset >= 10 else 5
            return {
                'data': [
                    {
                        'reviewId': f'MP_WXS_3075193430_{offset}_{i}',
                        'createTime': 1700000000 + offset + i,
                        'mpInfo': {'title': f'Article {offset}-{i}', 'pic_url': ''},
                    }
                    for i in range(size)
                ]
            }

        client.list_articles = list_articles  # type: ignore[assignment]
        storage = _FakeStorage()
        config = SyncConfig(
            mode=SyncMode.full,
            page_size=5,
            sleep_seconds=0,
            reset=False,
            recent_days=None,
            since_date=None,
            until_date=None,
            force=False,
            skip_minutes=None,
            download_content=False,
            download_images=False,
            content_limit=0,
            max_pages=None,
        )
        with patch('hippo.sync_core.open_storage', return_value=storage):
            summary = _run(
                sync_account_core(
                    storage=storage,
                    client=client,
                    account=_make_account(),
                    config=config,
                    plan=_make_plan(),
                    observer=NullSyncObserver(),
                )
            )
        # pages at offset 0, 5 (full), then 10 (short=2 -> stop). 3 fetches.
        self.assertEqual(client.calls, [0, 5, 10])
        self.assertEqual(len(storage.articles.saved), 12)
        self.assertTrue(summary.completed)


if __name__ == '__main__':
    unittest.main()
