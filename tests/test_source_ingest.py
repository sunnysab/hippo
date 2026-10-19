"""The list → articles → transcription jobs path shared by every media source."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hippo.media.pipeline import MediaJobSkipped, ResolvedMedia
from hippo.sources.ingest import ChannelInfo, Item, Listing, MediaResolverRegistry, MediaSync


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
        raw={'transcript_url': f'url-{article_id}'},
    )


class _FakeTransaction:
    async def __aenter__(self) -> _FakeTransaction:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeStorage:
    """Records what would be written; article pks are handed out in order."""

    def __init__(self) -> None:
        self.saved: list[list] = []
        self.queued: list[dict] = []
        #: (markdown, blocks) per article body written straight from a feed.
        self.contents: list[tuple[str, list[dict]]] = []
        self.commits = 0
        self._pks: dict[str, int] = {}

        self.meta = SimpleNamespace(get=AsyncMock(return_value=None))
        self.articles = AsyncMock()
        self.articles.save_articles = AsyncMock(side_effect=self._save)
        #: A subscription that already has a name and a picture: nothing to refresh.
        self.accounts = SimpleNamespace(
            get_account=AsyncMock(
                return_value=SimpleNamespace(nickname='Fireship', round_head_img='https://cdn/avatar.png')
            ),
            update_account_fields=AsyncMock(),
        )

        async def _save_content(article, **kwargs) -> int:
            self.contents.append((str(kwargs.get('content_markdown') or ''), kwargs.get('content_blocks') or []))
            return 1

        self.articles.save_article_content = AsyncMock(side_effect=_save_content)
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

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()


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
        # 列表阶段知道的额外信息（播客的转录稿地址等）跟着任务走
        self.assertEqual('url-a', storage.queued[0]['payload']['transcript_url'])
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

    async def test_a_feed_entry_with_a_body_is_stored_as_an_article(self) -> None:
        # 博客 feed（阮一峰那样的 Atom）：正文在 feed 里，没有音频 → 写正文、不排转写。
        storage = _FakeStorage()
        body = '<p>第一段</p><h2>小标题</h2><p>第二段</p>'
        source = _Source(
            [
                Listing(
                    items=[
                        Item(
                            article_id='post-1',
                            title='一篇博客',
                            link='https://blog/post-1',
                            media_url='',
                            content_html=body,
                        )
                    ]
                )
            ]
        )

        await MediaSync(storage=storage, source=source).sync_account(
            biz='podcast:blog', source_key='https://blog/atom.xml', pages=1
        )

        self.assertEqual([], storage.queued)          # 没有音频，不该排转写
        self.assertEqual(1, len(storage.contents))
        marks, blocks = storage.contents[0]
        self.assertIn('第一段', marks)
        self.assertIn('小标题', marks)
        self.assertTrue(blocks)

    async def test_an_audio_entry_still_queues(self) -> None:
        storage = _FakeStorage()

        await MediaSync(storage=storage, source=_Source([Listing(items=[_item('1')])])).sync_account(
            biz='podcast:1', source_key='key', pages=1
        )

        self.assertEqual(1, len(storage.queued))
        self.assertEqual([], storage.contents)

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

    async def test_the_page_ceiling_does_not_fetch_one_page_extra(self) -> None:
        storage = _FakeStorage()
        source = _Source(
            [
                Listing(items=[_item('a')], next_cursor='2', is_end=False),
                Listing(items=[_item('b')], next_cursor='3', is_end=False),
                Listing(items=[_item('c')], is_end=True),
            ]
        )

        stats = await MediaSync(storage=storage, source=source).sync_account(
            biz='podcast:x', source_key='feed', pages=2
        )

        self.assertEqual(2, stats.listed)
        # 第二页就是上限：第三页取回来也会被整页丢掉，不能再打一次列表接口。
        self.assertEqual(2, len(source.calls), 'the page past the ceiling is not fetched')


CHANNEL = 'UCsBjURrPoezykLs9EqgamOA'
AVATAR = 'https://yt3.googleusercontent.com/avatar=s88.jpg'


class _ChannelSource(_Source):
    """A source whose metadata is cheap enough for the sync to refresh (YouTube)."""

    def __init__(self, listings: list[Listing], info: ChannelInfo | None = None, error: Exception | None = None) -> None:
        super().__init__(listings)
        self._info = info
        self._error = error
        self.metadata_calls: list[str] = []

    async def channel_info(self, source_key: str) -> ChannelInfo:
        self.metadata_calls.append(source_key)
        if self._error is not None:
            raise self._error
        return self._info


class ProfileRefreshTest(unittest.IsolatedAsyncioTestCase):
    """A subscription made before its picture was available gets filled in."""

    def _sync(self, storage: _FakeStorage, source: _Source):
        return MediaSync(storage=storage, source=source).sync_account(
            biz=f'youtube:{CHANNEL}', source_key=CHANNEL
        )

    async def test_a_missing_picture_is_filled_from_the_channels_metadata(self) -> None:
        storage = _FakeStorage()
        storage.accounts.get_account = AsyncMock(
            return_value=SimpleNamespace(nickname=CHANNEL, round_head_img=None)
        )
        source = _ChannelSource(
            [Listing(items=[_item('a')])],
            info=ChannelInfo(biz=f'youtube:{CHANNEL}', nickname='Fireship', source_key=CHANNEL, avatar_url=AVATAR),
        )

        await self._sync(storage, source)

        self.assertEqual([CHANNEL], source.metadata_calls)
        storage.accounts.update_account_fields.assert_awaited_once_with(
            f'youtube:{CHANNEL}', round_head_img=AVATAR, nickname='Fireship'
        )

    async def test_a_name_someone_typed_is_kept(self) -> None:
        storage = _FakeStorage()
        storage.accounts.get_account = AsyncMock(
            return_value=SimpleNamespace(nickname='我自己起的名字', round_head_img=None)
        )
        source = _ChannelSource(
            [Listing(items=[_item('a')])],
            info=ChannelInfo(biz=f'youtube:{CHANNEL}', nickname='Fireship', source_key=CHANNEL, avatar_url=AVATAR),
        )

        await self._sync(storage, source)

        storage.accounts.update_account_fields.assert_awaited_once_with(f'youtube:{CHANNEL}', round_head_img=AVATAR)

    async def test_a_subscription_that_has_a_picture_costs_no_lookup(self) -> None:
        storage = _FakeStorage()
        source = _ChannelSource([Listing(items=[_item('a')])])

        await self._sync(storage, source)

        self.assertEqual([], source.metadata_calls)
        storage.accounts.update_account_fields.assert_not_awaited()

    async def test_a_source_without_channel_metadata_is_left_alone(self) -> None:
        storage = _FakeStorage()

        await self._sync(storage, _Source([Listing(items=[_item('a')])]))

        storage.accounts.get_account.assert_not_awaited()

    async def test_a_failed_refresh_does_not_fail_the_listing(self) -> None:
        storage = _FakeStorage()
        storage.accounts.get_account = AsyncMock(
            return_value=SimpleNamespace(nickname=CHANNEL, round_head_img=None)
        )
        source = _ChannelSource([Listing(items=[_item('a')])], error=RuntimeError('yt-dlp 失败（退出码 1）'))

        stats = await self._sync(storage, source)

        self.assertEqual(1, stats.listed)
        storage.accounts.update_account_fields.assert_not_awaited()

    async def test_an_account_row_that_is_gone_is_not_an_error(self) -> None:
        storage = _FakeStorage()
        storage.accounts.get_account = AsyncMock(side_effect=LookupError('Account not found'))
        source = _ChannelSource([Listing(items=[_item('a')])])

        stats = await self._sync(storage, source)

        self.assertEqual(1, stats.listed)
        self.assertEqual([], source.metadata_calls)

    async def test_a_profile_read_failure_keeps_the_listing(self) -> None:
        storage = _FakeStorage()
        storage.accounts.get_account = AsyncMock(side_effect=RuntimeError('db down'))
        source = _ChannelSource([Listing(items=[_item('a')])])

        stats = await self._sync(storage, source)

        self.assertEqual(1, stats.listed)
        self.assertEqual(1, len(storage.saved))
        self.assertEqual(1, storage.commits)
        storage.accounts.update_account_fields.assert_not_awaited()

    async def test_a_profile_write_failure_keeps_the_listing(self) -> None:
        storage = _FakeStorage()
        storage.accounts.get_account = AsyncMock(
            return_value=SimpleNamespace(nickname=CHANNEL, round_head_img=None)
        )
        storage.accounts.update_account_fields = AsyncMock(side_effect=RuntimeError('db down'))
        source = _ChannelSource(
            [Listing(items=[_item('a')])],
            info=ChannelInfo(biz=f'youtube:{CHANNEL}', nickname='Fireship', source_key=CHANNEL, avatar_url=AVATAR),
        )

        stats = await self._sync(storage, source)

        self.assertEqual(1, stats.listed)
        self.assertEqual(1, len(storage.saved))
        # The listing is committed before the refresh, so the write failure cannot lose it.
        self.assertEqual(1, storage.commits)
        storage.accounts.update_account_fields.assert_awaited_once()


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
