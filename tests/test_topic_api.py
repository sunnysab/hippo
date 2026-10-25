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
    async def test_one_call_fills_every_row(self):
        rows = [{'id': 1}, {'id': 2}, {'id': 3}]
        repo = SimpleNamespace(
            topics_for=AsyncMock(return_value={1: [{'slug': 'ai', 'name': '人工智能'}]})
        )

        await _attach_topics(SimpleNamespace(topics=repo), rows)

        repo.topics_for.assert_awaited_once_with([1, 2, 3])
        self.assertEqual([{'slug': 'ai', 'name': '人工智能'}], rows[0]['topics'])
        self.assertEqual([], rows[1]['topics'])
        self.assertEqual([], rows[2]['topics'])

    async def test_an_empty_page_costs_no_query(self):
        repo = SimpleNamespace(topics_for=AsyncMock(return_value={}))
        await _attach_topics(SimpleNamespace(topics=repo), [])
        repo.topics_for.assert_not_awaited()


    async def test_storage_without_a_topic_repository_degrades_to_empty(self):
        rows = [{'id': 1}]
        await _attach_topics(SimpleNamespace(), rows)
        self.assertEqual([], rows[0]['topics'])


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


class EventVisibilityTest(unittest.TestCase):
    def test_only_a_multi_publisher_event_is_served_by_the_feed(self):
        import asyncio

        from hippo.api.routers import feed as feed_api

        storage = SimpleNamespace(
            sessions=SimpleNamespace(resolve=AsyncMock(return_value=None)),
            topics=SimpleNamespace(public_event=AsyncMock(return_value=None)),
        )
        user = SimpleNamespace(id=1)
        request = SimpleNamespace(headers={}, url=SimpleNamespace(scheme='http'))

        with self.assertRaises(ApiError) as ctx:
            asyncio.run(
                feed_api.list_feed(
                    request,
                    limit=10,
                    event_id=9,
                    storage=storage,
                    user=user,
                )
            )
        # "Not found" rather than an empty list: an empty list would confirm the
        # event id exists without a second publisher behind it.
        self.assertEqual(404, ctx.exception.status)

    def test_a_visible_event_reaches_the_feed_query(self):
        import asyncio

        from hippo.api.routers import feed as feed_api

        storage = SimpleNamespace(
            topics=SimpleNamespace(public_event=AsyncMock(return_value={'id': 9, 'source_count': 2})),
        )
        user = SimpleNamespace(id=1)
        request = SimpleNamespace(headers={}, url=SimpleNamespace(scheme='http'))

        with patch.object(feed_api, '_list_feed', AsyncMock(return_value=[])) as listing:
            asyncio.run(
                feed_api.list_feed(request, limit=10, event_id=9, storage=storage, user=user)
            )

        self.assertEqual(9, listing.await_args.kwargs['event_id'])


class EventWhereClauseTest(unittest.TestCase):
    def test_no_event_adds_no_predicate(self):
        self.assertEqual(('', []), article_queries._build_event_where_clause(None))

    def test_the_predicate_targets_the_link_table_not_the_article(self):
        clause, params = article_queries._build_event_where_clause(9)
        self.assertIn('article_event_link', clause)
        self.assertIn('el.event_id = %s', clause)
        self.assertEqual([9], params)


class ArticleEventRouteTest(unittest.TestCase):
    """The list endpoint must accept event_id: the detail page links to it."""

    def _client(self, storage):
        app = FastAPI()
        install_exception_handlers(app)
        app.include_router(article_api.router, prefix='/api')
        app.dependency_overrides[deps.get_storage] = lambda: storage
        app.dependency_overrides[deps.current_user] = lambda: SimpleNamespace(id=1, username='u', role='user')
        return TestClient(app, raise_server_exceptions=False)

    def _payload(self):
        return {
            'articles': [],
            'page': 1,
            'page_size': 20,
            'total': 0,
            'item_show_type_facets': [],
            'topic_facets': [],
        }

    def _storage(self):
        return SimpleNamespace(
            users=SimpleNamespace(get_preferences=AsyncMock(return_value={})),
            topics=SimpleNamespace(public_event=AsyncMock(return_value={'id': 7, 'source_count': 2})),
        )

    def test_the_list_forwards_event_id_to_the_query(self):
        with (
            patch.object(article_api, 'get_site_settings', AsyncMock(return_value={'vocabulary': VOCABULARY})),
            patch.object(article_api, '_list_articles', AsyncMock(return_value=self._payload())) as listing,
        ):
            response = self._client(self._storage()).get('/api/article?event_id=7')

        self.assertEqual(200, response.status_code)
        # FastAPI silently drops unknown query params, so asserting the call is the
        # only way to know the filter actually reached the query.
        self.assertEqual(7, listing.await_args.kwargs['event_id'])

    def test_an_invisible_event_is_refused_like_the_feed_does(self):
        storage = self._storage()
        storage.topics.public_event = AsyncMock(return_value=None)
        with patch.object(article_api, 'get_site_settings', AsyncMock(return_value={'vocabulary': VOCABULARY})):
            response = self._client(storage).get('/api/article?event_id=99')
        self.assertEqual(404, response.status_code)

    def test_no_event_means_no_filter(self):
        with (
            patch.object(article_api, 'get_site_settings', AsyncMock(return_value={'vocabulary': VOCABULARY})),
            patch.object(article_api, '_list_articles', AsyncMock(return_value=self._payload())) as listing,
        ):
            self._client(self._storage()).get('/api/article')

        self.assertIsNone(listing.await_args.kwargs['event_id'])


class ListAppliesEventTest(unittest.IsolatedAsyncioTestCase):
    """The event predicate has to reach the SQL, not just the signature."""

    async def test_the_generated_query_carries_the_event_predicate(self):
        # First call is the list itself, then the two facet scans.
        with (
            patch.object(article_queries, 'fetchall_rows', AsyncMock(side_effect=[[], [], []])) as fetch,
            patch.object(article_queries, 'fetchone_row', AsyncMock(return_value={'total': 0})),
        ):
            await article_queries._list_articles(SimpleNamespace(), **_list_kwargs(event_id=7))

        sql, params = fetch.await_args_list[0].args[1], fetch.await_args_list[0].args[2]
        self.assertIn('article_event_link', sql)
        self.assertIn(7, params)


class EndToEndFacetPipelineTest(unittest.IsolatedAsyncioTestCase):
    """Route -> _list_articles -> facet SQL -> response body, nothing stubbed mid-way.

    The first review rejected this work because every layer was tested in isolation
    while the assembled path dropped the field. This test only doubles the database
    rows; the route, the query builder and the response model are the real ones.
    """

    async def test_topic_facets_survive_from_sql_to_the_response_body(self):
        import hippo.api.routers.article as article_api_module

        # The columns the real query selects, so the response model accepts the row.
        article_rows = [
            {'id': 1, 'biz': 'b1', 'article_id': 'a1', 'title': 'T1', 'item_show_type': 0,
             'author': None, 'digest': None, 'cover': None, 'link': 'https://example.com/a1',
             'source_url': None, 'publish_at': 1792916311, 'duration_seconds': None,
             'created_at': '2026-10-25T00:00:00Z', 'read_at': None, 'starred_at': None,
             'source': 'weixin', 'account_nickname': '某号', 'account_alias': None,
             'account_avatar': None, 'group_id': 1, 'group_name': '分组', 'image_id': None},
        ]
        topic_facet_rows = [{'slug': 'ai', 'total': 2}, {'slug': 'news', 'total': 1}]
        item_facet_rows = [{'item_show_type': 0, 'total': 1, 'grand_total': 1}]

        # Only the database rows are doubled; the route, the query builder and the
        # response model all run for real.
        async def fake_fetchall(storage, sql, params, **kwargs):
            if 'unnest' in sql:
                return topic_facet_rows
            if 'GROUP BY COALESCE' in sql:
                return item_facet_rows
            return article_rows

        # The route reads these through the real query module, so patch only at the
        # storage boundary and let everything else run.
        storage = SimpleNamespace(
            users=SimpleNamespace(get_preferences=AsyncMock(return_value={})),
            topics=SimpleNamespace(
                topics_for=AsyncMock(return_value={1: [{'slug': 'ai', 'name': '人工智能'}]}),
                public_event=AsyncMock(return_value=None),
            ),
        )

        with (
            patch.object(article_api_module, 'get_site_settings', AsyncMock(return_value={'vocabulary': VOCABULARY})),
            patch.object(article_queries, 'fetchall_rows', fake_fetchall),
            patch.object(article_queries, 'fetchone_row', AsyncMock(return_value={'total': 1})),
        ):
            app = FastAPI()
            install_exception_handlers(app)
            app.include_router(article_api_module.router, prefix='/api')
            app.dependency_overrides[deps.get_storage] = lambda: storage
            app.dependency_overrides[deps.current_user] = lambda: SimpleNamespace(
                id=1, username='u', role='user'
            )
            response = TestClient(app, raise_server_exceptions=False).get('/api/article?topic=ai')

        self.assertEqual(200, response.status_code)
        body = response.json()
        # The bar reads exactly this field; it being absent is what broke the feature.
        self.assertEqual(
            [{'slug': 'ai', 'name': '人工智能', 'count': 2},
             {'slug': 'news', 'name': '新闻与时事', 'count': 1}],
            body['topic_facets'],
        )
        self.assertEqual([{'slug': 'ai', 'name': '人工智能'}], body['articles'][0]['topics'])
