from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from hippo.repositories import _row_to_article
from hippo.server import ApiError, _build_article_query, _get_article, _normalize_item_show_type
from hippo.sync_tasks import _article_snapshot


class ArticleItemShowTypeTest(unittest.TestCase):
    def test_row_to_article_keeps_item_show_type(self) -> None:
        article = _row_to_article(
            {
                'biz': 'demo',
                'article_id': '100-1',
                'title': 'Picture Story',
                'item_show_type': 8,
                'author': 'hippo',
                'digest': 'digest',
                'cover': None,
                'link': 'https://mp.weixin.qq.com/s/demo',
                'source_url': None,
                'publish_at': 1710000000,
                'raw_json': json.dumps({'hello': 'world'}),
            }
        )

        self.assertEqual(article.item_show_type, 8)

    def test_article_snapshot_includes_item_show_type(self) -> None:
        article = _row_to_article(
            {
                'biz': 'demo',
                'article_id': '100-1',
                'title': 'Audio Story',
                'item_show_type': 7,
                'author': None,
                'digest': None,
                'cover': None,
                'link': 'https://mp.weixin.qq.com/s/audio',
                'source_url': None,
                'publish_at': 1710000000,
                'raw_json': json.dumps({}),
            }
        )

        snapshot = _article_snapshot(article)

        self.assertEqual(snapshot['item_show_type'], 7)

    def test_get_article_returns_item_show_type(self) -> None:
        article_row = {
            'id': 12,
            'biz': 'demo',
            'article_id': '100-1',
            'title': 'Music Story',
            'item_show_type': 6,
            'author': 'hippo',
            'digest': 'digest',
            'cover': None,
            'link': 'https://mp.weixin.qq.com/s/music',
            'source_url': None,
            'publish_at': 1710000000,
            'created_at': '2026-03-18T00:00:00+00:00',
            'account_nickname': 'Demo Account',
            'account_alias': None,
            'account_avatar': None,
            'group_id': None,
            'group_name': None,
        }
        content_row = {
            'content_json': [{'type': 'paragraph', 'text': 'hello'}],
            'clean_html': '<p>hello</p>',
            'updated_at': '2026-03-18T00:00:00+00:00',
        }

        with (
            patch('hippo.server.fetchone_row', side_effect=[article_row, content_row]),
            patch('hippo.server._get_visible_article_images', return_value=([], set())),
        ):
            payload = _get_article(storage=object(), article_id=12)

        self.assertEqual(payload['article']['item_show_type'], 6)
        self.assertEqual(payload['content_status'], 'ok')

    def test_build_article_query_applies_item_show_type_filter(self) -> None:
        query_sql, params = _build_article_query(
            storage=object(),
            group_id=None,
            biz='demo',
            item_show_type=8,
            query=None,
            since_ts=None,
            until_ts=None,
            sort_mode='publish_at_desc',
            limit=20,
            offset=0,
            article_id=None,
        )

        self.assertIn('a.item_show_type = %s', query_sql)
        self.assertEqual(params, ['demo', 8, 20, 0])

    def test_normalize_item_show_type_rejects_unknown_value(self) -> None:
        with self.assertRaises(ApiError):
            _normalize_item_show_type(99)


if __name__ == '__main__':
    unittest.main()
