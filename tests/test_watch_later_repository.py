"""Watch Later repository: what the SQL actually does.

The invariants the API leans on: appending keeps the first ``added_at`` and the
first position, removing deletes instead of flagging, and every mutation takes
the reader's row first so two concurrent appends cannot pick the same position.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

from hippo.repositories.watch_later import WatchLaterRepository

ADDED_AT = datetime(2026, 5, 2, 11, 0, tzinfo=UTC)


class _RecordingCursor:
    def __init__(self, *, row: dict[str, object] | None = None, rows: list[dict[str, object]] | None = None) -> None:
        self.executed: list[str] = []
        self.params: list[list[object]] = []
        self._row = row
        self._rows = rows or []

    async def __aenter__(self) -> _RecordingCursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, sql: str, params: object = None) -> None:
        self.executed.append(' '.join(sql.split()))
        self.params.append(list(params or ()))  # type: ignore[arg-type]

    async def executemany(self, sql: str, params_seq: object) -> None:
        for record in params_seq:  # type: ignore[union-attr]
            self.executed.append(' '.join(sql.split()))
            self.params.append(list(record))

    async def fetchone(self) -> dict[str, object] | None:
        return self._row

    async def fetchall(self) -> list[dict[str, object]]:
        return self._rows


def _repository(cursor: _RecordingCursor) -> WatchLaterRepository:
    return WatchLaterRepository(SimpleNamespace(cursor=lambda **kwargs: cursor))  # type: ignore[arg-type]


class LockTest(unittest.IsolatedAsyncioTestCase):
    async def test_lock_takes_the_reader_row(self) -> None:
        cursor = _RecordingCursor()

        await _repository(cursor).lock(user_id=7)

        self.assertEqual(['SELECT 1 FROM users WHERE id = %s FOR UPDATE'], cursor.executed)
        self.assertEqual([[7]], cursor.params)


class AppendTest(unittest.IsolatedAsyncioTestCase):
    async def test_append_locks_the_reader_then_keeps_the_first_entry(self) -> None:
        cursor = _RecordingCursor(row={'added_at': ADDED_AT})

        result = await _repository(cursor).add(user_id=7, article_pk=5)

        self.assertEqual(ADDED_AT.isoformat(), result)
        # The lock comes first: it is what makes the next statement's
        # MAX(position) safe against a concurrent append from the same reader.
        self.assertEqual('SELECT 1 FROM users WHERE id = %s FOR UPDATE', cursor.executed[0])
        self.assertIn('INSERT INTO article_watch_later', cursor.executed[1])
        self.assertIn('MAX(position)', cursor.executed[1])
        params = cursor.params[1]
        self.assertEqual(4, len(params))
        self.assertEqual([7, 5], params[:2])
        # The max() subquery is scoped to the same reader.
        self.assertEqual(7, params[3])

    async def test_a_second_append_does_not_move_or_restamp_the_entry(self) -> None:
        cursor = _RecordingCursor(row={'added_at': ADDED_AT})

        await _repository(cursor).add(user_id=7, article_pk=5)

        # Neither added_at nor position may be overwritten on conflict, or
        # re-adding an article would push it to the bottom of the list.
        self.assertIn('DO UPDATE SET added_at = article_watch_later.added_at', cursor.executed[1])
        self.assertNotIn('position = EXCLUDED.position', cursor.executed[1])

    async def test_a_missing_row_is_reported_as_no_timestamp(self) -> None:
        result = await _repository(_RecordingCursor(row=None)).add(user_id=7, article_pk=5)

        self.assertEqual('', result)


class RemoveTest(unittest.IsolatedAsyncioTestCase):
    async def test_removing_is_a_delete_not_a_flag(self) -> None:
        cursor = _RecordingCursor()

        result = await _repository(cursor).remove(user_id=7, article_pk=5)

        self.assertIsNone(result)
        self.assertEqual('SELECT 1 FROM users WHERE id = %s FOR UPDATE', cursor.executed[0])
        self.assertEqual('DELETE FROM article_watch_later WHERE user_id = %s AND article_pk = %s', cursor.executed[1])
        self.assertEqual([7, 5], cursor.params[1])


class EntriesTest(unittest.IsolatedAsyncioTestCase):
    async def test_entries_are_read_in_list_order_for_one_reader(self) -> None:
        rows = [
            {'article_pk': 3, 'added_at': ADDED_AT, 'position': 10},
            {'article_pk': 1, 'added_at': ADDED_AT, 'position': 30},
        ]
        cursor = _RecordingCursor(rows=rows)

        result = await _repository(cursor).entries(user_id=7)

        self.assertEqual(rows, result)
        self.assertIn('ORDER BY position ASC, article_pk ASC', cursor.executed[0])
        self.assertEqual([7], cursor.params[0])


class SetPositionsTest(unittest.IsolatedAsyncioTestCase):
    async def test_positions_are_written_for_the_caller_only(self) -> None:
        cursor = _RecordingCursor()

        await _repository(cursor).set_positions(user_id=7, assignments=[(3, 10), (1, 30)])

        self.assertEqual('SELECT 1 FROM users WHERE id = %s FOR UPDATE', cursor.executed[0])
        self.assertIn('UPDATE article_watch_later SET position = %s', cursor.executed[1])
        self.assertEqual([[10, 7, 3], [30, 7, 1]], cursor.params[1:])

    async def test_an_empty_move_writes_nothing(self) -> None:
        cursor = _RecordingCursor()

        await _repository(cursor).set_positions(user_id=7, assignments=[])

        self.assertEqual([], cursor.executed)


class AddedAtLookupTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_query_covers_a_whole_page_of_articles(self) -> None:
        cursor = _RecordingCursor(rows=[{'article_pk': 1, 'added_at': ADDED_AT}])

        result = await _repository(cursor).added_at_for(user_id=7, article_pks=[1, 2, 3])

        self.assertEqual({1: ADDED_AT.isoformat()}, result)
        self.assertEqual(1, len(cursor.executed))
        self.assertIn('ANY(%s)', cursor.executed[0])
        self.assertEqual([7, [1, 2, 3]], cursor.params[0])

    async def test_no_articles_means_no_query(self) -> None:
        cursor = _RecordingCursor()

        self.assertEqual({}, await _repository(cursor).added_at_for(user_id=7, article_pks=[]))
        self.assertEqual([], cursor.executed)


if __name__ == '__main__':
    unittest.main()
