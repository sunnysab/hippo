"""The push path writes the same article payload the list and detail paths do.

A pushed article is only "a new article" — its body still comes from the detail
call — so the queue payload is where its cover has to be right already, and it has
to pick the same crop the other two paths pick.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hippo.weixin_watch import enqueue_pushed_article

WIDE = 'https://cdn/wide-16x9.png'
MAIN = 'https://cdn/main.png'


class _Cursor:
    def __init__(self, biz: str | None) -> None:
        self._biz = biz

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def execute(self, sql: str, params: tuple) -> None:
        self.params = params

    async def fetchone(self) -> tuple[str] | None:
        return (self._biz,) if self._biz else None


class _Storage:
    def __init__(self, biz: str | None = 'Mz123') -> None:
        self.cursor = _Cursor(biz)
        self.conn = SimpleNamespace(cursor=lambda: self.cursor)
        self.queued: list[dict] = []
        self.article_queue = SimpleNamespace(enqueue_many=AsyncMock(side_effect=self._enqueue))
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def _enqueue(self, items) -> int:
        self.queued.extend(items)
        return len(self.queued)

    @property
    def payload(self) -> dict:
        return self.queued[0]['payload']


def _push(**overrides) -> SimpleNamespace:
    fields = {
        'gh_id': 'gh_1',
        'pub_name': '某号',
        'title': '一期',
        'url': 'https://mp.weixin.qq.com/s?__biz=Mz1&sn=abc',
        'digest': '摘要',
        'cover_url': MAIN,
        'publish_time': 1791417600,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class PushedCoverTest(unittest.TestCase):
    def test_the_wide_crop_of_the_event_wins(self) -> None:
        storage = _Storage()
        push = _push(
            raw_data={'cover_urls': [{'ratio': 'main', 'url': MAIN}, {'ratio': '16:9', 'url': WIDE}]}
        )

        added = asyncio.run(enqueue_pushed_article(storage, push))

        self.assertTrue(added)
        self.assertEqual(WIDE, storage.payload['cover_url'])
        self.assertEqual('article_push', storage.payload['source'])

    def test_a_variant_field_on_the_sdk_object_is_read_first(self) -> None:
        storage = _Storage()
        push = _push(cover_urls=[{'ratio': '16:9', 'url': WIDE}])

        asyncio.run(enqueue_pushed_article(storage, push))

        self.assertEqual(WIDE, storage.payload['cover_url'])

    def test_an_event_without_variants_keeps_its_single_cover(self) -> None:
        storage = _Storage()

        asyncio.run(enqueue_pushed_article(storage, _push()))

        self.assertEqual(MAIN, storage.payload['cover_url'])

    def test_an_event_with_variants_but_no_single_cover_still_gets_one(self) -> None:
        storage = _Storage()
        push = _push(cover_url='', raw_data={'cover_urls': [{'ratio': '16:9', 'url': WIDE}]})

        asyncio.run(enqueue_pushed_article(storage, push))

        self.assertEqual(WIDE, storage.payload['cover_url'])

    def test_an_event_with_no_cover_at_all_is_still_queued(self) -> None:
        storage = _Storage()
        push = _push(cover_url='', raw_data={'cover_urls': []})

        added = asyncio.run(enqueue_pushed_article(storage, push))

        self.assertTrue(added)
        # 没有封面就是没有（JSON null），不是空串：列表阶段的 payload 同样如此。
        self.assertIsNone(storage.payload['cover_url'])

    def test_an_unsubscribed_account_is_not_queued(self) -> None:
        storage = _Storage(biz=None)

        added = asyncio.run(enqueue_pushed_article(storage, _push()))

        self.assertFalse(added)
        self.assertEqual([], storage.queued)


if __name__ == '__main__':
    unittest.main()
