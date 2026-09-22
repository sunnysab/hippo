"""列表阶段的两个副作用：写回 ``last_synced_at``、缓存 daemon 解析出的 ``gh_id``。"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from hippo.sync_service import ArticleSyncService
from hippo.sync_types import NullSyncObserver, SyncConfig
from hippo.weixin_source import ListedArticles, QueuedArticle
from hippo.weixin_worker import SyncStats, WeixinArticleSync


class _FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[dict] = []

    def enqueue_many(self, items) -> int:
        for item in items:
            self.enqueued.append(item)
        return len(self.enqueued)


class _FakeAccounts:
    def __init__(self) -> None:
        self.synced: list[str] = []
        self.gh_ids: list[tuple[str, str]] = []

    def update_last_synced(self, biz: str) -> None:
        self.synced.append(biz)

    def set_gh_id(self, biz: str, gh_id: str) -> int:
        self.gh_ids.append((biz, gh_id))
        return 1

    def get_latest_publish_at(self, biz: str):
        return None


class _FakeStorage:
    def __init__(self) -> None:
        self.accounts = _FakeAccounts()
        self.article_queue = _FakeQueue()
        self.commits = 0

    def transaction(self):
        return self

    def commit(self) -> None:
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


class _FakeSource:
    def __init__(self, listed: ListedArticles) -> None:
        self._listed = listed
        self.calls: list[tuple[str, str, int]] = []

    async def list_articles(self, source_key: str, biz: str, pages: int = 1) -> ListedArticles:
        self.calls.append((source_key, biz, pages))
        return self._listed


class _FakeWeixinSync:
    def __init__(self, stats: SyncStats) -> None:
        self._stats = stats
        self.calls: list[dict] = []

    async def sync_account(self, *, biz: str, source_key: str, pages: int = 1) -> SyncStats:
        self.calls.append({'biz': biz, 'source_key': source_key, 'pages': pages})
        return self._stats


def _config(**overrides) -> SyncConfig:
    base = {
        'sleep_seconds': 15.0,
        'force': True,
        'skip_minutes': None,
    }
    base.update(overrides)
    return SyncConfig(**base)


class ListedArticlesTest(unittest.TestCase):
    def test_sync_account_caches_the_resolved_gh_id(self) -> None:
        listed = ListedArticles(
            items=[QueuedArticle(biz='MzA==', sn='sn-1', long_link='https://mp/s?sn=sn-1')],
            gh_id='gh_cached',
        )
        storage = _FakeStorage()
        source = _FakeSource(listed)
        sync = WeixinArticleSync(storage=storage, source=source, downloader=None)

        stats = asyncio.run(sync.sync_account(biz='MzA==', source_key='gh_cached', pages=1))

        self.assertEqual(stats.listed, 1)
        self.assertEqual(stats.enqueued, 1)
        self.assertEqual(storage.accounts.gh_ids, [('MzA==', 'gh_cached')])

    def test_sync_account_skips_gh_id_write_when_daemon_gives_none(self) -> None:
        listed = ListedArticles(
            items=[QueuedArticle(biz='MzA==', sn='sn-1', long_link='https://mp/s?sn=sn-1')],
            gh_id=None,
        )
        storage = _FakeStorage()
        sync = WeixinArticleSync(storage=storage, source=_FakeSource(listed), downloader=None)

        asyncio.run(sync.sync_account(biz='MzA==', source_key='SomeAlias', pages=1))

        self.assertEqual(storage.accounts.gh_ids, [])


class LastSyncedTest(unittest.TestCase):
    def test_listing_writes_last_synced_at(self) -> None:
        storage = _FakeStorage()
        weixin_sync = _FakeWeixinSync(SyncStats(listed=3, enqueued=2))
        service = ArticleSyncService(storage=storage, weixin_sync=weixin_sync)
        account = SimpleNamespace(
            biz='MzA==',
            nickname='Demo',
            is_disabled=False,
            last_synced_at=None,
            gh_id='gh_demo',
            alias='demo_alias',
            sync_mode=None,
            sync_recent_days=None,
            sync_interval_days=None,
        )

        result, summary = asyncio.run(
            service.sync_account(
                account=account,
                config=_config(),
                bulk=True,
                observer=NullSyncObserver(),
            )
        )

        self.assertFalse(result.failed)
        self.assertEqual(summary.total_saved, 2)
        self.assertEqual(storage.accounts.synced, ['MzA=='])
        self.assertEqual(weixin_sync.calls[0]['source_key'], 'gh_demo')

    def test_failed_listing_does_not_mark_synced(self) -> None:
        storage = _FakeStorage()

        class _FailingSync:
            async def sync_account(self, **_kwargs):
                raise RuntimeError('daemon down')

        service = ArticleSyncService(storage=storage, weixin_sync=_FailingSync())
        account = SimpleNamespace(
            biz='MzA==',
            nickname='Demo',
            is_disabled=False,
            last_synced_at=None,
            gh_id='gh_demo',
            alias=None,
            sync_mode=None,
            sync_recent_days=None,
            sync_interval_days=None,
        )

        result, summary = asyncio.run(
            service.sync_account(
                account=account,
                config=_config(),
                bulk=True,
                observer=NullSyncObserver(),
            )
        )

        self.assertTrue(result.failed)
        self.assertIsNone(summary)
        self.assertEqual(storage.accounts.synced, [])


if __name__ == '__main__':
    unittest.main()
