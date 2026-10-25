"""The direction filter: closed vocabulary, one batched topic lookup, own facet.

The behaviour worth pinning is what makes the filter safe and cheap: an unknown
slug is refused rather than silently ignored, the facets stay filtered by the
reader's own subscriptions, and a page of articles costs one extra query.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hippo import article_queries
from hippo.api import deps
from hippo.api.errors import install_exception_handlers
from hippo.api.routers import article as article_api
from hippo.article_queries import (
    _attach_topics,
    _build_topic_where_clause,
    _list_articles,
    _normalize_topic_filter,
)
from hippo.exceptions import ApiError
from hippo.topics import default_vocabulary

VOCABULARY = default_vocabulary()


def _list_kwargs(**overrides):
    kwargs = {
        'user_id': 1,
        'group_ids': None,
        'biz': None,
        'item_show_type': None,
        'query': None,
        'exclude_keywords': None,
        'since_ts': None,
        'until_ts': None,
        'sort_mode': 'publish_at_desc',
        'page': 1,
        'page_size': 20,
        'vocabulary': VOCABULARY,
    }
    kwargs.update(overrides)
    return kwargs


class NormalizeTest(unittest.TestCase):
    def test_a_slug_list_becomes_a_deduplicated_list(self):
        self.assertEqual(['ai', 'news'], _normalize_topic_filter('ai,news,ai', VOCABULARY))

    def test_blank_entries_mean_no_filter(self):
        for value in (None, '', ' , ,'):
            self.assertIsNone(_normalize_topic_filter(value, VOCABULARY))

    def test_an_unknown_slug_is_refused_with_400(self):
        for value in ('invented', 'ai,invented', 'AI'):
            with self.subTest(value=value), self.assertRaises(ApiError) as ctx:
                _normalize_topic_filter(value, VOCABULARY)
            self.assertEqual(400, ctx.exception.status)

    def test_an_oversized_filter_is_refused(self):
        with self.assertRaises(ApiError) as ctx:
            _normalize_topic_filter('ai,' * 200, VOCABULARY)
        self.assertEqual(400, ctx.exception.status)


class WhereClauseTest(unittest.TestCase):
    def test_no_topics_adds_no_predicate(self):
        self.assertEqual(('', []), _build_topic_where_clause(None))

    def test_the_predicate_is_an_exists_subquery_over_ready_rows_only(self):
        clause, params = _build_topic_where_clause(['ai', 'news'])
        # A join would shift the parameter order the join constants depend on.
        self.assertIn('EXISTS (SELECT 1 FROM article_topic', clause)
        self.assertIn("t.state = 'ready'", clause)
        self.assertIn('t.slugs && %s', clause)
        self.assertEqual([['ai', 'news']], params)


class FacetTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_facet_query_uses_the_same_where_clause_and_hides_other_subscriptions(self):
        seen = {}

        async def fake_fetchall(storage, sql, params, **kwargs):
            seen['sql'] = sql
            seen['params'] = params
            return []

        with patch.object(article_queries, 'fetchall_rows', fake_fetchall):
            await article_queries._article_topic_facets(
                storage=SimpleNamespace(),
                user_id=7,
                group_ids=None,
                biz=None,
                query=None,
                exclude_keywords=None,
                since_ts=None,
                until_ts=None,
                vocabulary=VOCABULARY,
            )

        # The subscription join is what keeps counts to what this reader can see.
        self.assertIn('JOIN subscription sub ON sub.biz = a.biz AND sub.user_id = %s', seen['sql'])
        self.assertIn('article_topic', seen['sql'])
        self.assertIn("t.state = 'ready'", seen['sql'])
        self.assertEqual(7, seen['params'][0])

    async def test_names_come_from_the_vocabulary_and_unknown_slugs_are_dropped(self):
        rows = [
            {'slug': 'ai', 'total': 3},
            {'slug': 'retired-direction', 'total': 2},
            {'slug': 'news', 'total': 1},
        ]
        with patch.object(article_queries, 'fetchall_rows', AsyncMock(return_value=rows)):
            facets = await article_queries._article_topic_facets(
                storage=SimpleNamespace(),
                user_id=1,
                group_ids=None,
                biz=None,
                query=None,
                exclude_keywords=None,
                since_ts=None,
                until_ts=None,
                vocabulary=VOCABULARY,
            )

        self.assertEqual(['ai', 'news'], [facet['slug'] for facet in facets])
        self.assertEqual('人工智能', facets[0]['name'])
        self.assertNotIn('retired-direction', [facet['slug'] for facet in facets])

    async def test_facets_follow_vocabulary_order_not_count_order(self):
        rows = [{'slug': 'news', 'total': 9}, {'slug': 'research', 'total': 1}]
        with patch.object(article_queries, 'fetchall_rows', AsyncMock(return_value=rows)):
            facets = await article_queries._article_topic_facets(
                storage=SimpleNamespace(),
                user_id=1,
                group_ids=None,
                biz=None,
                query=None,
                exclude_keywords=None,
                since_ts=None,
                until_ts=None,
                vocabulary=VOCABULARY,
            )
        order = [topic['slug'] for topic in VOCABULARY['topics']]
        self.assertEqual(sorted(order.index(f['slug']) for f in facets), [order.index(f['slug']) for f in facets])


class AttachTopicsTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_query_fills_every_row(self):
        rows = [{'id': 1}, {'id': 2}, {'id': 3}]
        stored = [{'article_pk': 1, 'topics': [{'slug': 'ai', 'name': '人工智能'}]}]
        calls = []

        async def fake_fetchall(storage, sql, params, **kwargs):
            calls.append(params[0])
            return stored

        with patch.object(article_queries, 'fetchall_rows', fake_fetchall):
            await _attach_topics(SimpleNamespace(), rows)

        self.assertEqual(1, len(calls))
        self.assertEqual([1, 2, 3], calls[0])
        self.assertEqual([{'slug': 'ai', 'name': '人工智能'}], rows[0]['topics'])
        self.assertEqual([], rows[1]['topics'])
        self.assertEqual([], rows[2]['topics'])

    async def test_an_empty_page_costs_no_query(self):
        with patch.object(article_queries, 'fetchall_rows', AsyncMock()) as fetch:
            await _attach_topics(SimpleNamespace(), [])
        fetch.assert_not_awaited()


class ListTotalTest(unittest.IsolatedAsyncioTestCase):
    async def test_selecting_a_direction_costs_one_more_count(self):
        facet_rows = [{'item_show_type': 0, 'total': 9, 'grand_total': 9}]
        with (
            patch.object(article_queries, 'fetchall_rows', AsyncMock(side_effect=[[], [], facet_rows])),
            patch.object(article_queries, 'fetchone_row', AsyncMock(return_value={'total': 2})) as count,
        ):
            result = await _list_articles(SimpleNamespace(), **_list_kwargs(topics=['ai']))

        # The facet total ignores its own filter, so it cannot be the answer here.
        count.assert_awaited_once()
        self.assertEqual(2, result['total'])

    async def test_without_a_direction_filter_the_facet_total_is_still_the_answer(self):
        facet_rows = [{'item_show_type': 0, 'total': 9, 'grand_total': 9}]
        with (
            patch.object(article_queries, 'fetchall_rows', AsyncMock(side_effect=[[], [], facet_rows])),
            patch.object(article_queries, 'fetchone_row', AsyncMock()) as count,
        ):
            result = await _list_articles(SimpleNamespace(), **_list_kwargs())

        count.assert_not_awaited()
        self.assertEqual(9, result['total'])


class RouteTest(unittest.TestCase):
    def _client(self, storage):
        app = FastAPI()
        install_exception_handlers(app)
        app.include_router(article_api.router, prefix='/api')
        app.dependency_overrides[deps.get_storage] = lambda: storage
        app.dependency_overrides[deps.current_user] = lambda: SimpleNamespace(id=1, username='u', role='user')
        return TestClient(app, raise_server_exceptions=False)

    def _storage(self):
        return SimpleNamespace(users=SimpleNamespace(get_preferences=AsyncMock(return_value={})))

    def test_an_unknown_direction_is_rejected_by_the_route(self):
        with patch.object(article_api, 'get_site_settings', AsyncMock(return_value={'vocabulary': VOCABULARY})):
            response = self._client(self._storage()).get('/api/article?topic=invented')
        self.assertEqual(400, response.status_code)

    def test_a_known_direction_reaches_the_query(self):
        payload = {
            'articles': [],
            'page': 1,
            'page_size': 20,
            'total': 0,
            'item_show_type_facets': [],
            'topic_facets': [],
        }
        with (
            patch.object(article_api, 'get_site_settings', AsyncMock(return_value={'vocabulary': VOCABULARY})),
            patch.object(article_api, '_list_articles', AsyncMock(return_value=payload)) as listing,
        ):
            response = self._client(self._storage()).get('/api/article?topic=ai,news')

        self.assertEqual(200, response.status_code)
        self.assertEqual(['ai', 'news'], listing.await_args.kwargs['topics'])
        self.assertEqual(VOCABULARY, listing.await_args.kwargs['vocabulary'])


class DocumentedShapeTest(unittest.TestCase):
    def test_the_response_model_publishes_topics_and_topic_facets(self):
        from hippo.api.schemas.article import ArticleListOut, ArticleOut

        self.assertIn('topics', ArticleOut.model_fields)
        self.assertIn('topic_facets', ArticleListOut.model_fields)


if __name__ == '__main__':
    unittest.main()
