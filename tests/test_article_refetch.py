"""Re-fetch: 重抓已有文章必须落回原行，而不是新建一份 adhoc 副本。

adhoc 只属于「手贴的、没有归属的链接」；重新抓取时 biz/article_id 取自原文，
upsert 的 (biz, article_id) 才会命中同一行。
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.api.routers import article as article_api
from hippo.downloader import ArticleDownloader, ArticleFetcher
from hippo.models import DownloadResult
from hippo.repositories.article import ArticleRepository


class DownloaderOwnershipTest(unittest.IsolatedAsyncioTestCase):
    async def _download(self, **kwargs) -> list:
        downloader = ArticleDownloader(client=SimpleNamespace(), storage=None)
        seen: list = []

        async def persist(article, *, raw_html, with_images, record_images_only):
            seen.append(article)
            return DownloadResult(article=article, asset_count=0)

        with (
            patch.object(ArticleFetcher, 'fetch_article_html', AsyncMock(return_value='<html></html>')),
            patch.object(ArticleDownloader, '_persist_article', side_effect=persist),
        ):
            await downloader.download_from_url('https://mp.weixin.qq.com/s/AbCdEf', with_images=False, **kwargs)
        return seen

    async def test_a_plain_url_download_still_lands_under_adhoc(self) -> None:
        seen = await self._download()

        self.assertEqual('adhoc', seen[0].biz)

    async def test_refetch_passes_the_original_owner_through(self) -> None:
        seen = await self._download(biz='Mz123', article_id='slug-from-the-database')

        # 从 URL 推出来的 token 不能盖掉原文的 article_id，否则还是新起一行。
        self.assertEqual('Mz123', seen[0].biz)
        self.assertEqual('slug-from-the-database', seen[0].article_id)


class RefetchRouteTest(unittest.TestCase):
    def test_the_endpoint_refetches_under_the_original_identity(self) -> None:
        row = {'biz': 'Mz123', 'article_id': 'slug-1', 'link': 'https://mp.weixin.qq.com/s/AbCdEf'}
        downloader = SimpleNamespace(download_from_url=AsyncMock())
        deferred: list = []

        class _DeferredThread:
            def __init__(self, target=None, daemon=None, **kwargs) -> None:
                self._target = target

            def start(self) -> None:
                deferred.append(self._target)

        class _Container:
            async def __aenter__(self):
                return SimpleNamespace(downloader=downloader)

            async def __aexit__(self, *exc):
                return False

        class _Storage:
            async def __aenter__(self):
                return SimpleNamespace()

            async def __aexit__(self, *exc):
                return False

        with (
            patch('hippo.api.routers.article.fetchone_row', AsyncMock(return_value=row)),
            patch('hippo.api.routers.article.open_storage', lambda: _Storage()),
            patch('hippo.api.routers.article.build_downloader_container', lambda **kwargs: _Container()),
            patch('hippo.api.routers.article.threading.Thread', _DeferredThread),
        ):
            task = asyncio.run(article_api.refetch_article(article_id=7, storage=SimpleNamespace()))
            # 后台线程的内容当场跑：此时没有别的事件循环
            self.assertEqual(1, len(deferred))
            deferred[0]()

        downloader.download_from_url.assert_awaited_once_with(
            'https://mp.weixin.qq.com/s/AbCdEf',
            with_images=True,
            biz='Mz123',
            article_id='slug-1',
        )
        self.assertEqual('done', article_api._refetch_tasks.pop(task['task_id'])['status'])


class ArticleUpsertTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_refetch_keeps_metadata_the_page_did_not_provide(self) -> None:
        queries: list[str] = []

        class _Cursor:
            async def execute(self, query, params=None) -> None:
                queries.append(query)

            async def fetchone(self):
                return (1, True)

        await ArticleRepository._upsert_article_row(
            _Cursor(),
            biz='Mz123',
            article_id='slug-1',
            title='t',
            item_show_type=None,
            author=None,
            digest=None,
            link='https://mp.weixin.qq.com/s/AbCdEf',
            source_url=None,
            publish_at=None,
            raw_json='{}',
            now=None,
        )

        # 传 None 的字段不能被清空：重抓的 stub 只有标题和链接。
        for column in ('author', 'digest', 'publish_at'):
            self.assertIn(f'{column}=COALESCE(EXCLUDED.{column}, articles.{column})', queries[0])


if __name__ == '__main__':
    unittest.main()
