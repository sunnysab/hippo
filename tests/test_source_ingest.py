"""The list → articles → transcription jobs path shared by every media source."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hippo.media.pipeline import MediaJobSkipped, ResolvedMedia
from hippo.sources.ingest import Item, Listing, MediaResolverRegistry, MediaSync


class _Source:
    source = 'podcast'

    def __init__(self, listings: list[Listing]) -> None:
        self._listings = listings
        self.calls: list[dict] = []

    async def list_items(self, *, source_key: str, limit: int, cursor: str | None = None) -> Listing:
        self.calls.append({'source_key': source_key, 'limit': limit, 'cursor': cursor})
        return self._listings.pop(0) if self._listings else Listing(items=[], is_end=True)


def _item(article_id: str) -> Item:
    return Item(
        article_id=article_id,
        title=f'条目 {article_id}',
        link=f'https://host/{article_id}',
        media_url=f'https://host/{article_id}.mp3',
        publish_at=1700000000,
    )


class _FakeStorage:
    """Records what would be written; article pks are handed out in order."""

    def __init__(self) -> None:
        self.saved: list[list] = []
        self.queued: list[dict] = []
        self.commits = 0
        self._pks: dict[str, int] = {}

        self.meta = SimpleNamespace(get=AsyncMock(return_value=None))
        self.articles = AsyncMock()
        self.articles.save_articles = AsyncMock(side_effect=self._save)
        self.media_jobs = AsyncMock()
        self.media_jobs.enqueue_many = AsyncMock(side_effect=self._enqueue)

        async def execute(*_args):
            rows = list(self._pks.items())

            class _Rows:
                async def fetchall(self):
                    return rows

            return _Rows()

        self.conn = SimpleNamespace(execute=execute)

    async def _save(self, records) -> int:
        records = list(records)
        self.saved.append(records)
        for record in records:
            self._pks.setdefault(record.article_id, len(self._pks) + 1)
        return len(records)

    async def _enqueue(self, items) -> int:
        known = {queued['article_pk'] for queued in self.queued}
        fresh = [item for item in items if item['article_pk'] not in known]
        self.queued.extend(fresh)
        return len(fresh)

    async def commit(self) -> None:
        self.commits += 1


class MediaSyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_listing_stores_articles_and_queues_transcription(self) -> None:
        storage = _FakeStorage()
        source = _Source([Listing(items=[_item('a'), _item('b')], is_end=True)])

        stats = await MediaSync(storage=storage, source=source).sync_account(biz='podcast:x', source_key='feed')

        self.assertEqual(2, stats.listed)
        self.assertEqual(2, stats.enqueued)
        self.assertEqual('条目 a', storage.saved[0][0].title)
        self.assertEqual('podcast', storage.queued[0]['source'])
        self.assertEqual('https://host/a.mp3', storage.queued[0]['media_url'])
        self.assertEqual(1, storage.commits)

    async def test_listing_the_same_items_again_queues_nothing(self) -> None:
        storage = _FakeStorage()
        source = _Source([Listing(items=[_item('a')], is_end=True), Listing(items=[_item('a')], is_end=True)])
        sync = MediaSync(storage=storage, source=source)

        await sync.sync_account(biz='podcast:x', source_key='feed')
        again = await sync.sync_account(biz='podcast:x', source_key='feed')

        self.assertEqual(1, again.listed)
        # media_job 的 UNIQUE(article_pk) + 入队守卫保证不会重复转写。
        self.assertEqual(0, again.enqueued)

    async def test_the_item_limit_comes_from_the_settings(self) -> None:
        storage = _FakeStorage()
        source = _Source([Listing(items=[], is_end=True)])

        await MediaSync(storage=storage, source=source).sync_account(biz='podcast:x', source_key='feed')

        self.assertEqual(5, source.calls[0]['limit'])

    async def test_a_cursor_walks_to_the_next_page(self) -> None:
        storage = _FakeStorage()
        source = _Source(
            [
                Listing(items=[_item('a')], next_cursor='2', is_end=False),
                Listing(items=[_item('b')], is_end=True),
            ]
        )

        stats = await MediaSync(storage=storage, source=source).sync_account(
            biz='podcast:x', source_key='feed', pages=2
        )

        self.assertEqual(2, stats.listed)
        # 第一次不带游标，第二次带上上一页给的游标。
        self.assertEqual([None, '2'], [call['cursor'] for call in source.calls])


class ResolverRegistryTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_job_source_picks_the_resolver(self) -> None:
        class _Resolver:
            async def resolve(self, job):
                return ResolvedMedia(audio_url='https://host/a.mp3')

        registry = MediaResolverRegistry({'bilibili': _Resolver()})

        media = await registry.resolve({'source': 'bilibili'})

        self.assertEqual('https://host/a.mp3', media.audio_url)

    async def test_an_unknown_source_is_a_skip_not_a_crash(self) -> None:
        registry = MediaResolverRegistry({'bilibili': object()})

        with self.assertRaises(MediaJobSkipped) as ctx:
            await registry.resolve({'source': 'youtube'})

        self.assertIn('youtube', str(ctx.exception))
        self.assertIn('bilibili', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
