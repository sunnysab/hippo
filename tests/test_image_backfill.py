"""What the image backfill actually sends to PostgreSQL, and through which exit.

The fetch query is assembled from string fragments, and one of them once dropped
the second argument of ``COALESCE(acc.source, )`` — a statement PostgreSQL refuses
outright, so every backfill run died before fetching an image. Asserting the name
of a constant would not have seen that, which is why these tests read the SQL the
calls hand to a recording cursor.
"""

import re
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from hippo import image_backfill
from hippo.image_backfill import backfill_article_images
from hippo.media_settings import default_media_settings


class _Cursor:
    """A psycopg cursor that answers the count and records every statement."""

    def __init__(self, *, total: int, rows: list[tuple]) -> None:
        self.total = total
        self._rows = rows
        self.executed: list[tuple[str, Any]] = []

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    async def fetchone(self) -> tuple[int]:
        return (self.total,)

    async def fetchall(self) -> list[tuple]:
        rows, self._rows = self._rows, []
        return rows


class _Storage:
    def __init__(self, *, total: int = 0, rows: list[tuple] | None = None) -> None:
        self.cursor = _Cursor(total=total, rows=rows or [])
        self.conn = SimpleNamespace(cursor=lambda: self.cursor)

    async def __aenter__(self) -> _Storage:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    @property
    def selects(self) -> list[tuple[str, Any]]:
        """The row statements (the ``COUNT(*)`` that sizes the run is not one)."""
        return [
            (sql, params)
            for sql, params in self.cursor.executed
            if 'FROM article_images' in sql and 'COUNT(*)' not in sql
        ]


class _Store:
    def __init__(self) -> None:
        self.stored: list[str] = []

    async def store(self, *, biz: str, article_id: str, orig_url: str, content_type: str | None, data: bytes) -> None:
        self.stored.append(orig_url)

    async def mark_failed(self, *, biz: str, article_id: str, orig_url: str, reason: str) -> None:
        raise AssertionError(f'should not fail: {orig_url} {reason}')


@contextmanager
def _backfill(storage: _Storage, store: _Store, client_class: type, settings: dict[str, Any]):
    with (
        patch.object(image_backfill, 'PostgresStorage', lambda _dsn: storage),
        patch.object(image_backfill, '_build_image_store', lambda _storage: store),
        patch.object(image_backfill, 'get_media_settings', AsyncMock(return_value=settings)),
        patch.object(image_backfill, 'MPClient', client_class),
    ):
        yield


def _row(image_id: int, *, biz: str, orig_url: str, source: str) -> tuple:
    return (image_id, biz, f'a{image_id}', 'https://mp.weixin.qq.com/s/x', orig_url, source)


class GeneratedQueryTest(unittest.IsolatedAsyncioTestCase):
    """The assembled statement must be one PostgreSQL can run."""

    async def test_the_source_of_each_image_comes_from_the_join(self) -> None:
        storage = _Storage(total=1, rows=[_row(1, biz='Mz1', orig_url='https://cdn/a.png', source='youtube')])

        with _backfill(storage, _Store(), object, default_media_settings()):
            result = await backfill_article_images(pg_dsn='postgres://test', dry_run=True)

        self.assertEqual(1, result['skipped'])
        self.assertEqual(1, len(storage.selects), 'the run reads one page')
        sql, params = storage.selects[0]
        # The default of the COALESCE is a real second argument, not an empty one.
        self.assertIn("COALESCE(acc.source, '')", sql)
        self.assertNotRegex(sql, r'COALESCE\(\s*acc\.source\s*,\s*\)')
        # A comma followed by the closing bracket is what a dropped fragment looks
        # like; the shipped query was exactly that.
        self.assertIsNone(re.search(r',\s*\)', sql), sql)
        self.assertEqual(sql.count('('), sql.count(')'), sql)
        self.assertIn('LEFT JOIN accounts acc ON acc.biz = a.biz', sql)
        # The statement and the arguments handed to psycopg belong together.
        self.assertEqual(sql.count('%s'), len(params))

    async def test_the_page_walk_passes_one_limit(self) -> None:
        storage = _Storage(total=2, rows=[_row(1, biz='Mz1', orig_url='https://cdn/a.png', source='weixin')])

        with _backfill(storage, _Store(), object, default_media_settings()):
            await backfill_article_images(pg_dsn='postgres://test', dry_run=True, limit=1)

        sql, params = storage.selects[0]
        self.assertTrue(sql.endswith('LIMIT %s'), sql)
        self.assertEqual((1,), params)


class SourceProxyTest(unittest.IsolatedAsyncioTestCase):
    """A YouTube image must not leave through the WeChat exit."""

    async def test_each_source_keeps_its_exit_and_an_unknown_one_uses_the_default(self) -> None:
        storage = _Storage(
            total=3,
            rows=[
                _row(1, biz='Mz1', orig_url='https://mmbiz.qpic.cn/wechat.png', source='weixin'),
                _row(2, biz='Mz2', orig_url='https://i.ytimg.com/vi/x/hq.jpg', source='youtube'),
                _row(3, biz='Mz3', orig_url='https://cdn/hand-added.png', source=''),
            ],
        )
        store = _Store()
        seen: dict[str, str] = {}

        class _Client:
            def __init__(self, *, proxy: str = '') -> None:
                self._proxy = proxy

            async def download_binary_with_type(self, url: str, *, referer: str | None = None):
                seen[url] = self._proxy
                return b'image-bytes', 'image/png'

            async def aclose(self) -> None:
                pass

        settings = {
            **default_media_settings(),
            'proxy_default': 'http://default:1',
            'proxy_youtube': 'http://youtube:2',
        }

        with _backfill(storage, store, _Client, settings):
            result = await backfill_article_images(pg_dsn='postgres://test')

        self.assertEqual(3, result['updated'])
        self.assertEqual(
            {
                'https://mmbiz.qpic.cn/wechat.png': '',
                'https://i.ytimg.com/vi/x/hq.jpg': 'http://youtube:2',
                'https://cdn/hand-added.png': 'http://default:1',
            },
            seen,
        )


class ClientLifetimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_client_opened_is_closed_again(self) -> None:
        storage = _Storage(
            total=2,
            rows=[
                _row(1, biz='Mz1', orig_url='https://a/1.png', source='weixin'),
                _row(2, biz='Mz2', orig_url='https://a/2.png', source='youtube'),
            ],
        )
        closed: list[str] = []
        opened: list[str] = []

        class _Client:
            def __init__(self, *, proxy: str = '') -> None:
                self._proxy = proxy
                opened.append(proxy)

            async def download_binary_with_type(self, url: str, *, referer: str | None = None):
                return b'image-bytes', 'image/png'

            async def aclose(self) -> None:
                closed.append(self._proxy)

        with _backfill(storage, _Store(), _Client, default_media_settings()):
            await backfill_article_images(pg_dsn='postgres://test')

        # One client per source, and the loop closes what it opened.
        self.assertEqual(2, len(opened))
        self.assertCountEqual(opened, closed)


if __name__ == '__main__':
    unittest.main()
