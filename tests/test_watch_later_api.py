"""Watch Later API: the ordered, per-user list.

The parts worth pinning down are the append that must keep the first position,
the move that has to step over entries the caller can no longer see, and the
transaction every write route opens — the request handle only closes, and
closing rolls the write back.
"""

import inspect
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from hippo import article_queries
from hippo.api.routers import article as article_api
from hippo.api.schemas.article import WatchLaterMoveRequest
from hippo.exceptions import ApiError

ADDED_AT = '2026-05-02T11:00:00+00:00'


def _user(user_id: int = 7) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, username='reader', role='user')


def _with_transaction(storage: SimpleNamespace) -> SimpleNamespace:
    """Give a storage double the commit the real request handle never does."""
    storage.commits = 0

    @asynccontextmanager
    async def transaction():
        yield storage
        storage.commits += 1

    storage.transaction = transaction
    return storage


def _storage(*, watch_later_at: str = ADDED_AT) -> SimpleNamespace:
    return _with_transaction(
        SimpleNamespace(
            watch_later=SimpleNamespace(
                add=AsyncMock(return_value=watch_later_at),
                remove=AsyncMock(return_value=None),
            )
        )
    )


def _entries(*pairs: tuple[int, int]) -> list[dict[str, object]]:
    """``(article_pk, position)`` pairs, the shape the repository returns."""
    return [{'article_pk': pk, 'position': position, 'added_at': ADDED_AT} for pk, position in pairs]


def _path_constraints(default: object) -> list[object]:
    return list(getattr(default, 'metadata', []))


class WatchLaterAuthTest(unittest.TestCase):
    def test_the_routes_require_a_session_like_the_rest_of_the_router(self) -> None:
        dependencies = [edge.dependency for edge in article_api.router.dependencies]
        self.assertIn(article_api.current_user, dependencies)

    def test_non_positive_ids_are_rejected_as_a_request_shape_error(self) -> None:
        # ``Path(gt=0)`` answers 422 before the route runs, so a bad id never
        # reaches a query.
        for route in (
            article_api.add_to_watch_later,
            article_api.remove_from_watch_later,
            article_api.move_in_watch_later,
        ):
            default = inspect.signature(route).parameters['article_id'].default
            constraints = _path_constraints(default)
            self.assertTrue(
                any(getattr(constraint, 'gt', None) == 0 for constraint in constraints),
                f'{route.__name__} must reject non-positive article ids, got {constraints!r}',
            )

    def test_an_unknown_direction_is_rejected_by_the_request_model(self) -> None:
        with self.assertRaises(ValidationError):
            WatchLaterMoveRequest.model_validate({'direction': 'left'})
        with self.assertRaises(ValidationError):
            WatchLaterMoveRequest.model_validate({})

    def test_the_three_directions_are_accepted(self) -> None:
        for direction in ('up', 'down', 'top'):
            self.assertEqual(direction, WatchLaterMoveRequest(direction=direction).direction)


class AddEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_adding_reports_the_first_time_and_commits(self) -> None:
        storage = _storage()

        with patch.object(article_api, '_assert_article_visible', AsyncMock()):
            result = await article_api.add_to_watch_later(article_id=5, storage=storage, user=_user())

        storage.watch_later.add.assert_awaited_once_with(user_id=7, article_pk=5)
        self.assertEqual(1, storage.commits)
        self.assertEqual({'article_id': 5, 'watch_later_at': ADDED_AT}, result)

    async def test_adding_twice_is_idempotent(self) -> None:
        storage = _storage()

        with patch.object(article_api, '_assert_article_visible', AsyncMock()):
            first = await article_api.add_to_watch_later(article_id=5, storage=storage, user=_user())
            second = await article_api.add_to_watch_later(article_id=5, storage=storage, user=_user())

        # The repository keeps the stored timestamp; the route must hand it back
        # instead of the time of this request.
        self.assertEqual(first, second)
        self.assertEqual(2, storage.commits)

    async def test_another_reader_writes_their_own_row(self) -> None:
        storage = _storage()

        with patch.object(article_api, '_assert_article_visible', AsyncMock()):
            await article_api.add_to_watch_later(article_id=5, storage=storage, user=_user(user_id=8))

        storage.watch_later.add.assert_awaited_once_with(user_id=8, article_pk=5)

    async def test_an_invisible_article_is_never_added(self) -> None:
        storage = _storage()

        with (
            patch.object(
                article_api, '_assert_article_visible', AsyncMock(side_effect=ApiError('not found', status=404))
            ),
            self.assertRaises(ApiError),
        ):
            await article_api.add_to_watch_later(article_id=5, storage=storage, user=_user())

        storage.watch_later.add.assert_not_awaited()
        self.assertEqual(0, storage.commits)


class RemoveEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_removing_reports_no_timestamp_and_commits(self) -> None:
        storage = _storage()

        result = await article_api.remove_from_watch_later(article_id=5, storage=storage, user=_user())

        storage.watch_later.remove.assert_awaited_once_with(user_id=7, article_pk=5)
        self.assertEqual(1, storage.commits)
        self.assertEqual({'article_id': 5, 'watch_later_at': None}, result)

    async def test_removing_does_not_ask_whether_the_article_is_visible(self) -> None:
        storage = _storage()

        with patch.object(article_api, '_assert_article_visible', AsyncMock()) as visible:
            await article_api.remove_from_watch_later(article_id=5, storage=storage, user=_user())

        # An entry stays removable after the reader lost sight of the article.
        visible.assert_not_awaited()
        storage.watch_later.remove.assert_awaited_once_with(user_id=7, article_pk=5)


class MoveEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_moving_hands_the_direction_to_the_query_layer_and_commits(self) -> None:
        storage = _with_transaction(SimpleNamespace())

        with patch.object(article_api, '_move_watch_later', AsyncMock()) as move:
            result = await article_api.move_in_watch_later(
                body=WatchLaterMoveRequest(direction='top'), article_id=5, storage=storage, user=_user()
            )

        move.assert_awaited_once_with(storage, 7, 5, 'top')
        self.assertEqual(1, storage.commits)
        self.assertEqual({'article_id': 5}, result)

    async def test_a_missing_entry_is_a_404_and_leaves_the_transaction_alone(self) -> None:
        storage = _storage()

        with (
            patch.object(
                article_api, '_move_watch_later', AsyncMock(side_effect=ApiError('Article not found', status=404))
            ),
            self.assertRaises(ApiError) as raised,
        ):
            await article_api.move_in_watch_later(
                body=WatchLaterMoveRequest(direction='up'), article_id=5, storage=storage, user=_user()
            )

        self.assertEqual(404, raised.exception.status)
        self.assertEqual(0, storage.commits)


class MoveOrderTest(unittest.IsolatedAsyncioTestCase):
    """Ordering is decided in the query layer, over the visible entries only."""

    def _list_storage(self, entries: list[dict[str, object]]) -> SimpleNamespace:
        calls: list[str] = []

        async def take_lock(**kwargs: object) -> None:
            calls.append('lock')

        async def read_entries(**kwargs: object) -> list[dict[str, object]]:
            calls.append('entries')
            return entries

        return SimpleNamespace(
            watch_later=SimpleNamespace(
                lock=AsyncMock(side_effect=take_lock),
                entries=AsyncMock(side_effect=read_entries),
                set_positions=AsyncMock(return_value=None),
            ),
            calls=calls,
        )

    async def _move(
        self,
        storage: SimpleNamespace,
        *,
        article_id: int,
        direction: str,
        visible: set[int],
    ) -> AsyncMock:
        with patch.object(article_queries, '_visible_article_pks', AsyncMock(return_value=visible)):
            await article_queries._move_watch_later(storage, 7, article_id, direction)
        return storage.watch_later.set_positions

    async def test_the_reader_is_locked_before_the_list_is_read(self) -> None:
        storage = self._list_storage(_entries((1, 10), (2, 20)))

        await self._move(storage, article_id=2, direction='up', visible={1, 2})

        # The new order is computed from this snapshot, so the lock has to be
        # held before it is taken; locking inside the write would be too late.
        self.assertEqual(['lock', 'entries'], storage.calls)
        storage.watch_later.lock.assert_awaited_once_with(user_id=7)

    async def test_the_lock_is_taken_even_when_nothing_will_change(self) -> None:
        storage = self._list_storage(_entries((1, 10), (2, 20)))

        await self._move(storage, article_id=1, direction='up', visible={1, 2})

        self.assertEqual(['lock', 'entries'], storage.calls)
        storage.watch_later.set_positions.assert_not_awaited()

    async def test_up_steps_over_an_invisible_entry(self) -> None:
        storage = self._list_storage(_entries((1, 10), (2, 20), (3, 30)))

        set_positions = await self._move(storage, article_id=3, direction='up', visible={1, 3})

        # Entry 2 is not readable, so it neither blocks the move nor changes.
        set_positions.assert_awaited_once_with(user_id=7, assignments=[(3, 10), (1, 30)])
        storage.watch_later.entries.assert_awaited_once_with(user_id=7)

    async def test_down_steps_over_an_invisible_entry(self) -> None:
        storage = self._list_storage(_entries((1, 10), (2, 20), (3, 30)))

        set_positions = await self._move(storage, article_id=1, direction='down', visible={1, 3})

        set_positions.assert_awaited_once_with(user_id=7, assignments=[(3, 10), (1, 30)])

    async def test_top_moves_to_the_front_keeping_the_rest_in_order(self) -> None:
        storage = self._list_storage(_entries((1, 10), (2, 20), (3, 30)))

        set_positions = await self._move(storage, article_id=3, direction='top', visible={1, 2, 3})

        set_positions.assert_awaited_once_with(user_id=7, assignments=[(3, 10), (1, 20), (2, 30)])

    async def test_moving_the_first_entry_up_changes_nothing(self) -> None:
        storage = self._list_storage(_entries((1, 10), (2, 20)))

        set_positions = await self._move(storage, article_id=1, direction='up', visible={1, 2})

        set_positions.assert_not_awaited()

    async def test_moving_the_last_entry_down_changes_nothing(self) -> None:
        storage = self._list_storage(_entries((1, 10), (2, 20)))

        set_positions = await self._move(storage, article_id=2, direction='down', visible={1, 2})

        set_positions.assert_not_awaited()

    async def test_an_entry_the_caller_cannot_see_is_a_404(self) -> None:
        storage = self._list_storage(_entries((1, 10), (2, 20)))

        with self.assertRaises(ApiError) as raised:
            await self._move(storage, article_id=2, direction='up', visible={1})

        self.assertEqual(404, raised.exception.status)
        storage.watch_later.set_positions.assert_not_awaited()

    async def test_an_article_that_is_not_on_the_list_is_a_404(self) -> None:
        storage = self._list_storage(_entries((1, 10)))

        with self.assertRaises(ApiError) as raised:
            await self._move(storage, article_id=9, direction='top', visible={1, 9})

        self.assertEqual(404, raised.exception.status)
        storage.watch_later.set_positions.assert_not_awaited()


class VisibilityTest(unittest.TestCase):
    def test_the_queue_reuses_the_article_gate_instead_of_inventing_one(self) -> None:
        """``_assert_article_visible`` grants a subscription *or* a current direct
        reference. A watch-later entry may only be listed and moved under the same
        join, or the queue would offer cards the article route refuses."""
        for function in (article_queries._visible_article_pks, article_queries._assert_article_visible):
            self.assertIn('ARTICLE_ACCESS_JOIN', inspect.getsource(function))

    def test_the_list_query_applies_that_gate(self) -> None:
        self.assertIn('ARTICLE_ACCESS_JOIN', inspect.getsource(article_queries._list_watch_later))


class BadgeTest(unittest.IsolatedAsyncioTestCase):
    """``watch_later_at`` on a page of articles costs one query, not one per row."""

    async def test_a_page_of_rows_costs_one_lookup(self) -> None:
        storage = SimpleNamespace(
            watch_later=SimpleNamespace(added_at_for=AsyncMock(return_value={1: ADDED_AT}))
        )
        rows = [{'id': 1}, {'id': 2}, {'id': 3}]

        await article_queries._attach_watch_later(storage, 7, rows)

        storage.watch_later.added_at_for.assert_awaited_once_with(user_id=7, article_pks=[1, 2, 3])
        self.assertEqual([ADDED_AT, None, None], [row['watch_later_at'] for row in rows])

    async def test_a_storage_without_the_repository_leaves_the_rows_alone(self) -> None:
        rows = [{'id': 1}]

        await article_queries._attach_watch_later(SimpleNamespace(), 7, rows)

        self.assertNotIn('watch_later_at', rows[0])


class ListQueryTest(unittest.TestCase):
    def test_the_list_is_scoped_to_one_reader_and_ordered_by_position(self) -> None:
        # The SQL is assembled from shared fragments, so the two things a
        # regression would break are asserted on the source itself.
        source = inspect.getsource(article_queries._list_watch_later)
        self.assertIn('WHERE wl.user_id = %s', source)
        self.assertIn('ORDER BY wl.position ASC', source)
        self.assertIn('ARTICLE_ACCESS_JOIN', source)
        # No feed filter and no personal exclude keywords: this list is built by
        # hand, so the list route decides what is in it.
        self.assertNotIn('exclude_keywords', source)
        self.assertNotIn('group_ids', source)


if __name__ == '__main__':
    unittest.main()
