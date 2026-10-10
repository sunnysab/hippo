"""Source adapter registry.

Adding a source must stay a data change: the sync service picks an adapter by
``accounts.source``, and an account whose source has no adapter is reported as a
skip with a reason instead of taking the whole round down.
"""

import asyncio
import contextlib
import unittest
from types import SimpleNamespace

from hippo.models import AccountCredential
from hippo.sources import UnsupportedSource, adapter_for, body_adapters
from hippo.sync_service import ArticleSyncService
from hippo.sync_types import NullSyncObserver, SyncConfig
from hippo.weixin_worker import SyncStats


class _Adapter:
    def __init__(self, stats: SyncStats | None = None) -> None:
        self.calls: list[dict] = []
        self._stats = stats or SyncStats()

    async def sync_account(self, *, biz: str, source_key: str, pages: int = 1, **_kwargs) -> SyncStats:
        self.calls.append({'biz': biz, 'source_key': source_key, 'pages': pages})
        return self._stats


class _BodyAdapter(_Adapter):
    def __init__(self) -> None:
        super().__init__()
        self.drained = 0

    async def drain(self, *, limit: int | None = None) -> SyncStats:
        self.drained += 1
        return SyncStats(ingested=1)


class _FakeStorage:
    def __init__(self) -> None:
        self.synced: list[str] = []

        async def update_last_synced(biz: str) -> None:
            self.synced.append(biz)

        self.accounts = SimpleNamespace(update_last_synced=update_last_synced)

    def transaction(self):
        return contextlib.nullcontext()


def _account(**overrides) -> SimpleNamespace:
    values = {
        'biz': 'x:1',
        'nickname': 'Demo',
        'source': 'weixin',
        'is_disabled': False,
        'last_synced_at': None,
        'gh_id': None,
        'alias': 'key-1',
        'sync_mode': None,
        'sync_recent_days': None,
        'sync_interval_days': None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _config() -> SyncConfig:
    return SyncConfig(sleep_seconds=0.0, force=True, skip_minutes=None)


class RegistryTest(unittest.TestCase):
    def test_an_adapter_is_found_by_source(self) -> None:
        adapter = _Adapter()

        self.assertIs(adapter, adapter_for({'bilibili': adapter}, 'bilibili'))

    def test_a_missing_adapter_names_what_is_registered(self) -> None:
        with self.assertRaises(UnsupportedSource) as ctx:
            adapter_for({'weixin': _Adapter()}, 'bilibili')

        self.assertIn('bilibili', str(ctx.exception))
        self.assertIn('weixin', str(ctx.exception))

    def test_only_adapters_with_a_body_queue_are_drained(self) -> None:
        body = _BodyAdapter()

        self.assertEqual([body], body_adapters({'weixin': body, 'podcast': _Adapter()}))


class AccountSourceDefaultTest(unittest.TestCase):
    def test_the_model_defaults_to_weixin(self) -> None:
        # 既有数据只有微信；新来源必须显式给 source，否则会被静默标错。
        account = AccountCredential(biz='b', nickname='n')

        self.assertEqual('weixin', account.source)

    def test_an_explicit_source_is_kept(self) -> None:
        account = AccountCredential(biz='b', nickname='n', source='podcast')

        self.assertEqual('podcast', account.source)


class ServiceDispatchTest(unittest.TestCase):
    def _run(self, adapters, account):
        service = ArticleSyncService(storage=_FakeStorage(), adapters=adapters)
        return asyncio.run(
            service.sync_account(
                account=account,
                config=_config(),
                bulk=True,
                observer=NullSyncObserver(),
            )
        )

    def test_the_matching_adapter_is_used(self) -> None:
        weixin = _Adapter(SyncStats(listed=3, enqueued=2))
        bilibili = _Adapter()

        result, summary = self._run({'weixin': weixin, 'bilibili': bilibili}, _account(source='bilibili'))

        self.assertFalse(result.failed)
        self.assertEqual([], weixin.calls)
        self.assertEqual(1, len(bilibili.calls))
        self.assertEqual('key-1', bilibili.calls[0]['source_key'])
        self.assertEqual(0, summary.total_saved)

    def test_an_unregistered_source_is_skipped_not_failed(self) -> None:
        result, summary = self._run({'weixin': _Adapter()}, _account(source='bilibili'))

        self.assertFalse(result.failed)
        self.assertTrue(result.skipped)
        self.assertEqual('unsupported_source', result.skip_reason)
        self.assertIsNone(summary)


if __name__ == '__main__':
    unittest.main()
