"""Auto and manual sync policy: per-source intervals, manual force, cooldown."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from hippo.sources.weixin import WeixinSourceAdapter
from hippo.sync_core import reset_sync_cancel
from hippo.sync_service import ArticleSyncService
from hippo.sync_types import NullSyncObserver, SyncConfig
from hippo.utils import resolve_sync_interval
from hippo.weixin_worker import SyncStats


class _Accounts:
    def __init__(self, latest_publish_at: datetime | None = None) -> None:
        self.latest_publish_at = latest_publish_at
        self.synced: list[str] = []

    async def get_latest_publish_at(self, biz: str) -> datetime | None:
        return self.latest_publish_at

    async def update_last_synced(self, biz: str) -> None:
        self.synced.append(biz)


class _Storage:
    def __init__(self, latest_publish_at: datetime | None = None) -> None:
        self.accounts = _Accounts(latest_publish_at)

    def transaction(self) -> _Storage:
        return self

    async def __aenter__(self) -> _Storage:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _RecordingAdapter:
    def __init__(self, stats: SyncStats) -> None:
        self.stats = stats
        self.calls: list[str] = []

    async def sync_account(self, *, biz: str, source_key: str, pages: int = 1, offset: str = '') -> SyncStats:
        self.calls.append(biz)
        return self.stats


class _FailingAdapter:
    async def sync_account(self, *, biz: str, source_key: str, pages: int = 1, offset: str = '') -> SyncStats:
        raise RuntimeError('daemon down')


def _account(
    biz: str = 'b',
    *,
    source: str = 'weixin',
    last_synced_at: datetime | None = None,
    sync_interval_days: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        biz=biz,
        nickname=biz,
        source=source,
        is_disabled=False,
        last_synced_at=last_synced_at,
        gh_id='gh',
        alias=None,
        sync_mode=None,
        sync_recent_days=None,
        sync_interval_days=sync_interval_days,
    )


class ResolveSyncIntervalTest(unittest.TestCase):
    def test_an_explicit_interval_wins_on_every_source(self) -> None:
        for source in ('weixin', 'youtube', 'podcast'):
            account = _account(source=source, sync_interval_days=7)

            self.assertEqual(asyncio.run(resolve_sync_interval(_Storage(), account)), 7)

    def test_other_sources_default_to_every_scheduled_check(self) -> None:
        account = _account(source='youtube')

        # The publish-history lookup is never consulted for a non-WeChat source.
        self.assertEqual(asyncio.run(resolve_sync_interval(_Storage(), account)), 1)

    def test_weixin_keeps_the_publish_history_interval(self) -> None:
        account = _account(source='weixin')
        old_publish = datetime.now(UTC) - timedelta(days=200)

        self.assertGreater(asyncio.run(resolve_sync_interval(_Storage(old_publish), account)), 1)


class ManualRunPolicyTest(unittest.TestCase):
    def _service(self, adapter) -> ArticleSyncService:
        return ArticleSyncService(
            storage=_Storage(),
            adapters={'weixin': WeixinSourceAdapter(adapter)},
        )

    def test_manual_skips_the_daily_interval(self) -> None:
        adapter = _RecordingAdapter(SyncStats(listed=1, enqueued=1))
        account = _account(last_synced_at=datetime.now(UTC), sync_interval_days=30)
        config = SyncConfig(sleep_seconds=0, force=True, skip_minutes=None)

        result, _ = asyncio.run(
            self._service(adapter).sync_account(account=account, config=config, bulk=True, observer=NullSyncObserver())
        )

        self.assertFalse(result.skipped)
        self.assertEqual(adapter.calls, ['b'])

    def test_manual_keeps_the_short_cooldown(self) -> None:
        adapter = _RecordingAdapter(SyncStats(listed=1, enqueued=1))
        account = _account(last_synced_at=datetime.now(UTC))
        config = SyncConfig(sleep_seconds=0, force=True, skip_minutes=30)

        result, _ = asyncio.run(
            self._service(adapter).sync_account(account=account, config=config, bulk=True, observer=NullSyncObserver())
        )

        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, 'recently_synced')
        self.assertEqual(adapter.calls, [])


class PerSourceFailureIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_sync_cancel()

    def test_a_weixin_outage_does_not_stop_other_sources(self) -> None:
        ok = _RecordingAdapter(SyncStats(listed=1, enqueued=1))
        service = ArticleSyncService(
            storage=_Storage(),
            adapters={'weixin': _FailingAdapter(), 'youtube': ok},
        )
        accounts = [
            _account('wx1', source='weixin'),
            _account('wx2', source='weixin'),
            _account('wx3', source='weixin'),
            _account('wx4', source='weixin'),
            _account('yt1', source='youtube'),
        ]
        config = SyncConfig(sleep_seconds=0, force=True, skip_minutes=None)

        report = asyncio.run(service.sync_accounts(accounts=accounts, config=config, bulk=True))

        self.assertEqual(ok.calls, ['yt1'])
        completed = [detail.biz for detail in report.details if detail.completed]
        self.assertEqual(completed, ['yt1'])
        # The fourth WeChat account is skipped once that source is blocked.
        self.assertNotIn('wx4', [detail.biz for detail in report.details])


if __name__ == '__main__':
    unittest.main()
