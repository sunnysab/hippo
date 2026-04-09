from __future__ import annotations

import json
import unittest
from unittest.mock import ANY, patch

from hippo.repositories import _row_to_article
from hippo.server import (
    ApiError,
    _build_article_query,
    _count_articles,
    _count_article_item_show_type_facets,
    _get_article,
    _list_articles,
    _normalize_item_show_type,
)
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

    def test_get_article_normalizes_null_item_show_type_to_regular_article(self) -> None:
        article_row = {
            'id': 12,
            'biz': 'demo',
            'article_id': '100-1',
            'title': 'Regular Story',
            'item_show_type': None,
            'author': 'hippo',
            'digest': 'digest',
            'cover': None,
            'link': 'https://mp.weixin.qq.com/s/demo',
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

        self.assertEqual(payload['article']['item_show_type'], 0)

    def test_build_article_query_applies_item_show_type_filter(self) -> None:
        query_sql, params = _build_article_query(
            storage=object(),
            group_id=None,
            biz='demo',
            item_show_type=8,
            query=None,
            exclude_keywords=None,
            since_ts=None,
            until_ts=None,
            sort_mode='publish_at_desc',
            limit=20,
            offset=0,
            article_id=None,
        )

        self.assertIn('a.item_show_type = %s', query_sql)
        self.assertEqual(params, ['demo', 8, 20, 0])

    def test_build_article_query_treats_null_as_regular_article(self) -> None:
        query_sql, params = _build_article_query(
            storage=object(),
            group_id=None,
            biz='demo',
            item_show_type=0,
            query=None,
            exclude_keywords=None,
            since_ts=None,
            until_ts=None,
            sort_mode='publish_at_desc',
            limit=20,
            offset=0,
            article_id=None,
        )

        self.assertIn('(a.item_show_type = %s OR a.item_show_type IS NULL)', query_sql)
        self.assertEqual(params, ['demo', 0, 20, 0])

    def test_build_article_query_applies_exclude_keywords_filter(self) -> None:
        query_sql, params = _build_article_query(
            storage=object(),
            group_id=None,
            biz='demo',
            item_show_type=None,
            query=None,
            exclude_keywords=['promo', 'ad'],
            since_ts=None,
            until_ts=None,
            sort_mode='publish_at_desc',
            limit=20,
            offset=0,
            article_id=None,
        )

        self.assertIn('LOWER(COALESCE(a.title, \'\')) LIKE %s', query_sql)
        self.assertIn('LOWER(COALESCE(a.digest, \'\')) LIKE %s', query_sql)
        self.assertIn('LOWER(COALESCE(a.author, \'\')) LIKE %s', query_sql)
        self.assertIn('NOT (', query_sql)
        self.assertEqual(
            params,
            [
                'demo',
                '%promo%',
                '%promo%',
                '%promo%',
                '%ad%',
                '%ad%',
                '%ad%',
                20,
                0,
            ],
        )

    def test_count_article_item_show_type_facets_filters_invalid_rows(self) -> None:
        rows = [
            {'item_show_type': 8, 'total': 5},
            {'item_show_type': '0', 'total': '12'},
            {'item_show_type': 99, 'total': 7},
            {'item_show_type': 17, 'total': 0},
            {'item_show_type': None, 'total': 9},
            {'item_show_type': 6, 'total': 3},
        ]

        with patch('hippo.server.fetchall_rows', return_value=rows) as fetchall_rows:
            facets = _count_article_item_show_type_facets(
                storage=object(),
                group_id=42,
                biz='demo',
                query='picture',
                exclude_keywords=None,
                since_ts=1710000000,
                until_ts=1710086400,
                article_id='100-1',
            )

        self.assertEqual(
            facets,
            [
                {'item_show_type': 0, 'count': 21},
                {'item_show_type': 6, 'count': 3},
                {'item_show_type': 8, 'count': 5},
            ],
        )
        _, query_sql, params = fetchall_rows.call_args.args
        self.assertIn('GROUP BY COALESCE(a.item_show_type, 0)', query_sql)
        self.assertIn('ORDER BY COALESCE(a.item_show_type, 0) ASC', query_sql)
        self.assertNotIn('a.item_show_type = %s', query_sql)
        self.assertEqual(params[0], '100-1')
        self.assertIn(42, params)
        self.assertIn('demo', params)
        self.assertIn(1710000000, params)
        self.assertIn(1710086400, params)

    def test_count_article_item_show_type_facets_maps_null_to_regular_article(self) -> None:
        rows = [
            {'item_show_type': None, 'total': 9},
            {'item_show_type': 8, 'total': 5},
        ]

        with patch('hippo.server.fetchall_rows', return_value=rows):
            facets = _count_article_item_show_type_facets(
                storage=object(),
                group_id=None,
                biz=None,
                query=None,
                exclude_keywords=None,
                since_ts=None,
                until_ts=None,
                article_id=None,
            )

        self.assertEqual(
            facets,
            [
                {'item_show_type': 0, 'count': 9},
                {'item_show_type': 8, 'count': 5},
            ],
        )

    def test_list_articles_includes_item_show_type_facets(self) -> None:
        storage = object()
        article_rows = [{'id': 1, 'biz': 'demo', 'article_id': '100-1', 'title': 'Picture Story', 'item_show_type': None}]
        facets = [
            {'item_show_type': 0, 'count': 12},
            {'item_show_type': 8, 'count': 5},
        ]

        with (
            patch('hippo.server._build_article_query', return_value=('SELECT 1', ['demo'])) as build_query,
            patch('hippo.server.fetchall_rows', return_value=article_rows) as fetchall_rows,
            patch('hippo.server._count_articles', return_value=17) as count_articles,
            patch(
                'hippo.server._count_article_item_show_type_facets',
                return_value=facets,
            ) as count_facets,
        ):
            payload = _list_articles(
                storage=storage,
                group_id=3,
                biz='demo',
                item_show_type=8,
                query='picture',
                exclude_keywords=None,
                since_ts=None,
                until_ts=None,
                sort_mode='publish_at_desc',
                page=2,
                page_size=20,
                article_id=None,
            )

        self.assertEqual(payload['articles'][0]['account_avatar_url'], '/api/account/demo/avatar')
        self.assertEqual(payload['articles'][0]['item_show_type'], 0)
        self.assertEqual(payload['total'], 17)
        self.assertEqual(payload['item_show_type_facets'], facets)
        build_query.assert_called_once()
        fetchall_rows.assert_called_once_with(storage, 'SELECT 1', ['demo'], normalize=ANY)
        count_articles.assert_called_once()
        count_facets.assert_called_once_with(
            storage=storage,
            group_id=3,
            biz='demo',
            query='picture',
            exclude_keywords=None,
            since_ts=None,
            until_ts=None,
            article_id=None,
        )

    def test_list_articles_passes_exclude_keywords_to_counts_and_facets(self) -> None:
        storage = object()
        article_rows = [{'id': 1, 'biz': 'demo', 'article_id': '100-1', 'title': 'Normal Story', 'item_show_type': None}]

        with (
            patch('hippo.server._build_article_query', return_value=('SELECT 1', ['demo'])) as build_query,
            patch('hippo.server.fetchall_rows', return_value=article_rows),
            patch('hippo.server._count_articles', return_value=1) as count_articles,
            patch('hippo.server._count_article_item_show_type_facets', return_value=[]) as count_facets,
        ):
            _list_articles(
                storage=storage,
                group_id=3,
                biz='demo',
                item_show_type=None,
                query='story',
                exclude_keywords=['promo'],
                since_ts=None,
                until_ts=None,
                sort_mode='publish_at_desc',
                page=1,
                page_size=20,
                article_id=None,
            )

        build_query.assert_called_once_with(
            storage=storage,
            group_id=3,
            biz='demo',
            item_show_type=None,
            query='story',
            exclude_keywords=['promo'],
            since_ts=None,
            until_ts=None,
            sort_mode='publish_at_desc',
            limit=20,
            offset=0,
            article_id=None,
        )
        count_articles.assert_called_once_with(
            storage=storage,
            group_id=3,
            biz='demo',
            item_show_type=None,
            query='story',
            exclude_keywords=['promo'],
            since_ts=None,
            until_ts=None,
            article_id=None,
        )
        count_facets.assert_called_once_with(
            storage=storage,
            group_id=3,
            biz='demo',
            query='story',
            exclude_keywords=['promo'],
            since_ts=None,
            until_ts=None,
            article_id=None,
        )

    def test_count_articles_treats_null_as_regular_article(self) -> None:
        with patch('hippo.server.fetchone_row', return_value={'total': 12}) as fetchone_row:
            total = _count_articles(
                storage=object(),
                group_id=None,
                biz='demo',
                item_show_type=0,
                query=None,
                exclude_keywords=None,
                since_ts=None,
                until_ts=None,
                article_id=None,
            )

        self.assertEqual(total, 12)
        _, query_sql, params = fetchone_row.call_args.args
        self.assertIn('(a.item_show_type = %s OR a.item_show_type IS NULL)', query_sql)
        self.assertEqual(params, ['demo', 0])

    def test_normalize_item_show_type_rejects_unknown_value(self) -> None:
        with self.assertRaises(ApiError):
            _normalize_item_show_type(99)


if __name__ == '__main__':
    unittest.main()
