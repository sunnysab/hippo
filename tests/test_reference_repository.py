"""SQL contracts for reference identity lookup, retries and publication."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hippo.repositories.reference import ArticleReferenceRepository
from hippo.wechat_links import parse_wechat_link


class ReferenceRepositoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cursor = AsyncMock()
        self.cursor.__aenter__.return_value = self.cursor
        self.repo = ArticleReferenceRepository(SimpleNamespace(cursor=lambda **kwargs: self.cursor))
        self.link = parse_wechat_link('https://mp.weixin.qq.com/s?__biz=Mz1&sn=abc')

    async def test_all_identities_are_resolved_in_one_parameterized_query(self):
        self.cursor.fetchall.return_value = [{'identity': self.link.identity, 'id': 2, 'ready': True}]
        result = await self.repo.resolve([self.link])
        self.assertEqual(2, result[self.link.identity]['id'])
        self.cursor.execute.assert_awaited_once()
        sql, params = self.cursor.execute.await_args.args
        self.assertIn('jsonb_to_recordset(%s)', sql)
        self.assertIn("raw_json::jsonb->>'sn' = input.sn", sql)
        self.assertIn('article_id = input.slug', sql)
        self.assertIn("jsonb_typeof(c.content_json) = 'array'", sql)
        self.assertEqual(self.link.identity, params[0].obj[0]['identity'])
        self.assertNotIn(self.link.url, sql)

    async def test_no_links_does_not_query_the_database(self):
        self.assertEqual({}, await self.repo.resolve([]))
        self.assertEqual({}, await self.repo.states([]))
        self.cursor.execute.assert_not_awaited()

    async def test_regular_open_and_explicit_retry_have_different_queue_flags(self):
        self.cursor.fetchone.return_value = (12,)
        for retry in (False, True):
            self.cursor.execute.reset_mock()
            self.assertEqual(12, await self.repo.enqueue(self.link, article_pk=1, retry=retry))
            insert, binding = self.cursor.execute.await_args_list
            self.assertEqual((retry, retry, retry), insert.args[1][-3:])
            self.assertIn("article_queue.state = 'failed'", insert.args[0])
            self.assertNotIn("state = 'processing'", insert.args[0])
            self.assertEqual((12, 1, self.link.identity), binding.args[1])
            self.assertIn('WHERE source_article_pk = %s', binding.args[0])

    async def test_register_binds_the_current_source_version_and_removes_old_urls(self):
        await self.repo.register(1, [self.link], {self.link.identity: {'id': 2}})
        delete, insert = self.cursor.execute.await_args_list
        self.assertEqual((1, [self.link.identity]), delete.args[1])
        self.assertIn('SELECT article_pk, updated_at', insert.args[0])
        self.assertEqual((self.link.url, self.link.identity, 2, 1), insert.args[1])

    async def test_completion_binds_all_sources_sharing_the_queue_item(self):
        await self.repo.complete(12, 2)
        sql, params = self.cursor.execute.await_args.args
        self.assertIn('WHERE queue_id = %s', sql)
        self.assertEqual((2, 12), params)

    async def test_publisher_registration_does_not_subscribe_or_overwrite_existing_accounts(self):
        await self.repo.ensure_publisher(biz='Mz1', nickname='Publisher')
        sql, params = self.cursor.execute.await_args.args
        self.assertIn('ON CONFLICT (biz) DO NOTHING', sql)
        self.assertIn("'done'", sql)
        self.assertNotIn('subscription', sql)
        self.assertEqual('Mz1', params[0])
