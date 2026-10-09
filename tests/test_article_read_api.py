"""Read-state API and query invariants.

Read state is a side table joined by (user, article), so the things worth
pinning down are the ownership check, the one write path, and the parameter
order of the join that carries it into the list query.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.api.routers import article as article_api
from hippo.article_queries import (
    _build_article_query,
    _build_article_where_clause,
    _normalize_read_filter,
)
from hippo.exceptions import ApiError
from hippo.repositories.read import ArticleReadRepository


def _user(user_id: int = 7) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, username='reader', role='user')


def _storage(*, read_at: str | None = '2026-05-02T11:00:00+00:00') -> SimpleNamespace:
    return SimpleNamespace(
        reads=SimpleNamespace(set_read=AsyncMock(return_value=read_at)),
        users=SimpleNamespace(get_preferences=AsyncMock(return_value={})),
    )


class ReadFilterTest(unittest.TestCase):
    def test_only_unread_is_a_filter(self) -> None:
        self.assertFalse(_normalize_read_filter(None))
        self.assertFalse(_normalize_read_filter(''))
        self.assertTrue(_normalize_read_filter('unread'))

    def test_an_unknown_filter_is_rejected_instead_of_ignored(self) -> None:
        with self.assertRaises(ApiError) as raised:
            _normalize_read_filter('read')
        self.assertEqual(400, raised.exception.status)

    def test_the_unread_predicate_needs_the_read_join(self) -> None:
        where_sql, params, _tsquery, _params = _build_article_where_clause(
            group_ids=None,
            biz=None,
            item_show_type=None,
            query=None,
            exclude_keywords=None,
            since_ts=None,
            until_ts=None,
            unread_only=True,
        )

        self.assertIn('ar.read_at IS NULL', where_sql)
        self.assertEqual([], params)


class ReadJoinParamsTest(unittest.TestCase):
    def _query(self, *, unread_only: bool) -> tuple[str, list[object]]:
        return _build_article_query(
            storage=SimpleNamespace(),
            user_id=42,
            group_ids=None,
            biz=None,
            item_show_type=None,
            query=None,
            exclude_keywords=None,
            since_ts=None,
            until_ts=None,
            sort_mode='publish_at_desc',
            limit=20,
            offset=0,
            unread_only=unread_only,
        )

    def test_read_at_comes_back_for_every_row(self) -> None:
        sql, _params = self._query(unread_only=False)
        self.assertIn('ar.read_at', sql)
        self.assertIn('LEFT JOIN article_read ar ON ar.article_pk = a.id AND ar.user_id = %s', sql)

    def test_both_joins_bind_the_caller_id_before_the_where_clause(self) -> None:
        sql, params = self._query(unread_only=True)

        # The subscription join binds first, the read join second, then the
        # WHERE values, then LIMIT/OFFSET.
        self.assertEqual([42, 42, 20, 0], params)
        self.assertLess(sql.index('subscription sub'), sql.index('article_read ar'))
        self.assertLess(sql.index('article_read ar'), sql.index('ar.read_at IS NULL'))


class SetReadEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_marking_read_reports_the_read_time(self) -> None:
        storage = _storage(read_at='2026-05-02T11:00:00+00:00')

        with patch.object(article_api, '_assert_article_visible', AsyncMock()):
            result = await article_api.set_article_read(
                article_id=5, body={'read': True}, storage=storage, user=_user()
            )

        storage.reads.set_read.assert_awaited_once_with(user_id=7, article_pk=5, read=True)
        self.assertEqual({'article_id': 5, 'read_at': '2026-05-02T11:00:00+00:00'}, result)

    async def test_marking_unread_reports_no_read_time(self) -> None:
        storage = _storage(read_at=None)

        with patch.object(article_api, '_assert_article_visible', AsyncMock()):
            result = await article_api.set_article_read(
                article_id=5, body={'read': False}, storage=storage, user=_user()
            )

        storage.reads.set_read.assert_awaited_once_with(user_id=7, article_pk=5, read=False)
        self.assertIsNone(result['read_at'])

    async def test_a_non_boolean_read_is_rejected(self) -> None:
        storage = _storage()

        with patch.object(article_api, '_assert_article_visible', AsyncMock()), self.assertRaises(ApiError) as raised:
            await article_api.set_article_read(article_id=5, body={'read': 'yes'}, storage=storage, user=_user())

        self.assertEqual(400, raised.exception.status)
        storage.reads.set_read.assert_not_awaited()

    async def test_an_invisible_article_is_never_marked(self) -> None:
        storage = _storage()

        with (
            patch.object(
                article_api, '_assert_article_visible', AsyncMock(side_effect=ApiError('not found', status=404))
            ),
            self.assertRaises(ApiError),
        ):
            await article_api.set_article_read(article_id=5, body={'read': True}, storage=storage, user=_user())

        storage.reads.set_read.assert_not_awaited()


class ReadRepositoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_marking_unread_deletes_instead_of_flagging(self) -> None:
        executed: list[str] = []

        class _Cursor:
            rowcount = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, sql, params=None):
                executed.append(' '.join(sql.split()))

            async def fetchone(self):
                return None

        conn = SimpleNamespace(cursor=lambda **kwargs: _Cursor())
        repository = ArticleReadRepository(conn)

        result = await repository.set_read(user_id=1, article_pk=2, read=False)

        self.assertIsNone(result)
        self.assertEqual(['DELETE FROM article_read WHERE user_id = %s AND article_pk = %s'], executed)

    async def test_rereading_keeps_the_first_read_time(self) -> None:
        executed: list[str] = []

        class _Cursor:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, sql, params=None):
                executed.append(' '.join(sql.split()))

            async def fetchone(self):
                return {'read_at': None}

        conn = SimpleNamespace(cursor=lambda **kwargs: _Cursor())
        repository = ArticleReadRepository(conn)

        await repository.set_read(user_id=1, article_pk=2, read=True)

        # The conflict branch must not overwrite read_at, or reopening an
        # article would keep pushing its read time forward.
        self.assertIn('DO UPDATE SET read_at = article_read.read_at', executed[0])


if __name__ == '__main__':
    unittest.main()
