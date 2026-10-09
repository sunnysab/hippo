"""ImageDownloadManager: 图片要并发下载，且不能把协程当结果交出去。

一次重抓如果带着图片，`download_images_for_article` 里写 `tg.create_task(await …)` 会把
并发退化成串行，并且 create_task(None) 直接抛 TypeError —— 对外表现是
"unhandled errors in a TaskGroup"，正文已经写进去了，用户却只看到一个失败。
"""

import asyncio
import unittest

from hippo.downloader import ImageDownloadManager
from hippo.models import ArticleRecord


def _article() -> ArticleRecord:
    return ArticleRecord(
        biz='Mz123',
        article_id='a1',
        title='t',
        author=None,
        digest=None,
        cover=None,
        link='https://mp.weixin.qq.com/s/AbCdEf',
        source_url=None,
        publish_at=None,
        raw={},
    )


class _Client:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.seen: list[str] = []

    async def download_binary_with_type(self, url: str, *, referer: str | None = None):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.seen.append(url)
        await asyncio.sleep(0.01)
        self.active -= 1
        return b'image-bytes', 'image/png'


class _Store:
    def __init__(self) -> None:
        self.stored: list[str] = []

    async def store(self, *, biz, article_id, orig_url, content_type, data) -> None:
        self.stored.append(orig_url)

    async def mark_failed(self, *, biz, article_id, orig_url, reason) -> None:
        raise AssertionError(f'should not fail: {orig_url} {reason}')


class ImageDownloadManagerTest(unittest.IsolatedAsyncioTestCase):
    async def test_images_are_downloaded_concurrently(self) -> None:
        client = _Client()
        store = _Store()
        manager = ImageDownloadManager(client=client, image_store=store, storage=None, workers=4)
        urls = ['https://img.example/1.png', 'https://img.example/2.png', 'https://img.example/3.png']

        await manager.enqueue(_article(), {url: url for url in urls}, referer='https://mp.weixin.qq.com/')

        self.assertEqual(sorted(client.seen), sorted(urls))
        self.assertEqual(sorted(store.stored), sorted(urls))
        # 串行实现（await 后再 create_task）峰值只会是 1。
        self.assertGreaterEqual(client.peak, 2)
        self.assertEqual(await manager.stats(), (len(urls), len(urls)))


if __name__ == '__main__':
    unittest.main()
