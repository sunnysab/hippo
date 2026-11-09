"""Export archives must be portable and stay within the caller's subscriptions."""

import asyncio
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.api.routers import article as api
from hippo.exceptions import ApiError


class ArticleExportTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.storage = SimpleNamespace()
        self.user = SimpleNamespace(id=7)
        self.rows = [self.article(1, 'one'), self.article(2, 'two')]
        self.image_rows = [
            {'id': 11, 'orig_url': 'https://mmbiz.qpic.cn/image'},
            {'id': 12, 'orig_url': 'https://mmbiz.qpic.cn/image-large'},
            {'id': 13, 'orig_url': 'https://mmbiz.qpic.cn/blocked'},
        ]
        self.page_params = []

    def article(self, pk: int, biz: str) -> dict:
        return {
            'id': pk,
            'biz': biz,
            'account_nickname': '../account',
            'title': '../../title',
            'author': None,
            'digest': None,
            'link': 'https://mp.weixin.qq.com/article',
            'source_url': None,
            'publish_at': 123,
            'content_markdown': (
                '![stored](https://mmbiz.qpic.cn/image)\n'
                '![pending](https://mmbiz.qpic.cn/image-large)\n'
                '![blocked](https://mmbiz.qpic.cn/blocked)'
            ),
            'content_json': '[{"type": "text", "text": "hello"}, {"type": "image", "image_id": 13}]',
        }

    async def query(self, storage, sql, params):
        self.assertIs(storage, self.storage)
        if 'FROM subscription WHERE' in sql:
            self.assertEqual([7, ['one', 'two']], params)
            return [{'biz': 'one'}, {'biz': 'two'}]
        if 'FROM articles a' in sql:
            # Disabled subscriptions are still exportable.
            self.assertNotIn('is_disabled', sql)
            self.page_params.append(params)
            return self.rows if params[-1] == 0 else []
        if 'FROM article_images' in sql:
            return self.image_rows
        if 'FROM article_media' in sql:
            return [{'kind': 'video', 'content_type': 'video/mp4', 's3_key': 'private/video.bin'}]
        self.fail(sql)

    async def image_bytes(self, storage, image_id, *, allow_origin_fetch):
        self.assertFalse(allow_origin_fetch)
        if image_id == 11:
            return b'image bytes', 'image/png'
        raise RuntimeError('Not stored')

    def media_bytes(self, key, path):
        self.assertEqual('private/video.bin', key)
        path.write_bytes(b'video bytes')

    async def export(self, format='markdown'):
        with (
            patch.object(api, 'fetchall_rows', side_effect=self.query),
            patch.object(api, '_get_visible_article_images', AsyncMock(return_value=(self.image_rows[:2], {13}))),
            patch.object(api, 'fetch_image_bytes', side_effect=self.image_bytes) as images,
            patch.object(api.archive_module, 'fetch_object', side_effect=self.media_bytes),
        ):
            response = await api.export_articles(
                biz=['one', 'two', 'one'],
                format=format,
                storage=self.storage,
                user=self.user,
            )
        self.assertEqual([11, 12, 11, 12], [call.args[1] for call in images.await_args_list])
        return response

    async def test_markdown_zip_contains_one_file_per_article_and_relative_attachments(self) -> None:
        response = await self.export()
        try:
            self.assertEqual('application/zip', response.media_type)
            with zipfile.ZipFile(response.path) as archive:
                names = archive.namelist()
                articles = [name for name in names if name.endswith('/article.md')]
                self.assertEqual(2, len(articles))
                self.assertFalse(any('..' in Path(name).parts or name.startswith('/') for name in names))
                for name in articles:
                    markdown = archive.read(name).decode()
                    self.assertIn('![stored](attachments/images/11.png)', markdown)
                    self.assertIn('![pending](https://mmbiz.qpic.cn/image-large)', markdown)
                    self.assertIn('(attachments/media/video.mp4)', markdown)
                    self.assertNotIn('blocked', markdown)
                    self.assertNotIn('private/', markdown)
                    parent = str(Path(name).parent)
                    self.assertEqual(b'image bytes', archive.read(f'{parent}/attachments/images/11.png'))
                    self.assertEqual(b'video bytes', archive.read(f'{parent}/attachments/media/video.mp4'))
                self.assertTrue(all(info.compress_type == zipfile.ZIP_DEFLATED for info in archive.infolist()))
            self.assertEqual([[7, ['one', 'two'], 0], [7, ['one', 'two'], 2]], self.page_params)
        finally:
            await response.background()
        self.assertFalse(Path(response.path).parent.exists())

    async def test_json_also_uses_a_zip_with_separate_articles(self) -> None:
        response = await self.export('json')
        try:
            with zipfile.ZipFile(response.path) as archive:
                articles = [name for name in archive.namelist() if name.endswith('/article.json')]
                self.assertEqual(2, len(articles))
                article = json.loads(archive.read(articles[0]))
                self.assertEqual([{'type': 'text', 'text': 'hello'}], article['content'])
                self.assertIn('![stored](attachments/images/11.png)', article['content_markdown'])
                self.assertNotIn('blocked', article['content_markdown'])
                self.assertEqual(['attachments/images/11.png', 'attachments/media/video.mp4'], article['attachments'])
        finally:
            await response.background()

    async def test_unsubscribed_accounts_abort_before_creating_files(self) -> None:
        with (
            patch.object(api, 'fetchall_rows', AsyncMock(return_value=[{'biz': 'one'}])),
            patch.object(api.tempfile, 'mkdtemp') as create_dir,
            self.assertRaises(ApiError) as raised,
        ):
            await api.export_articles(biz=['one', 'other'], storage=self.storage, user=self.user)
        self.assertEqual(404, raised.exception.status)
        create_dir.assert_not_called()

    async def test_errors_remove_incomplete_archives(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            work = Path(root) / 'export'
            work.mkdir()
            with (
                patch.object(api.tempfile, 'mkdtemp', return_value=str(work)),
                patch.object(
                    api,
                    'fetchall_rows',
                    AsyncMock(
                        side_effect=[
                            [{'biz': 'one'}],
                            RuntimeError('query failed'),
                        ]
                    ),
                ),
                self.assertRaisesRegex(RuntimeError, 'query failed'),
            ):
                await api.export_articles(biz=['one'], storage=self.storage, user=self.user)
            self.assertFalse(work.exists())

    async def test_cancellation_waits_for_the_file_writer(self) -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def write():
            started.set()
            release.wait(timeout=5)
            finished.set()

        task = asyncio.create_task(api._export_thread_call(write))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())
