"""Which turns ``ChatRepository.list_messages`` returns, and in what order.

The SQL runs against an in-memory sqlite database wrapped in a very thin async
cursor, so a wrong window (leading instead of trailing) or a wrong order fails
here. A mock that returns a prepared list could never catch that.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta

from hippo.repositories.chat import ChatRepository

_SCHEMA = """
CREATE TABLE chat_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL
)
"""

_START = datetime(2024, 1, 1, 9, 0, 0)


class _Cursor:
    """Async facade over a sqlite3 cursor, mirroring the psycopg calls used.

    shortcut: only ``execute``/``fetchall`` exist because that is all
    ``list_messages`` uses; add methods when the repository calls more.
    """

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    async def execute(self, query: str, params: tuple = ()) -> None:
        self._cursor.execute(query.replace('%s', '?'), params)

    async def fetchall(self) -> list[dict]:
        columns = [column[0] for column in self._cursor.description]
        rows = [dict(zip(columns, row, strict=True)) for row in self._cursor.fetchall()]
        # psycopg hands back real datetimes; sqlite hands back the TEXT we wrote.
        for row in rows:
            if isinstance(row.get('created_at'), str):
                row['created_at'] = datetime.fromisoformat(row['created_at'])
        return rows

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _Connection:
    """Stamp for the psycopg connection; sqlite rows are already dict-shaped."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def cursor(self, *, row_factory=None) -> _Cursor:
        return _Cursor(self._connection.cursor())


class ListMessagesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._sqlite = sqlite3.connect(':memory:')
        self._sqlite.executescript(_SCHEMA)
        self.repository = ChatRepository(_Connection(self._sqlite))
        self._clock = 0

    def tearDown(self) -> None:
        self._sqlite.close()

    def _add(self, session_id: int, content: str) -> int:
        self._clock += 1
        cursor = self._sqlite.execute(
            'INSERT INTO chat_message (session_id, role, content, created_at) VALUES (?, ?, ?, ?)',
            (session_id, 'user', content, (_START + timedelta(seconds=self._clock)).isoformat()),
        )
        return int(cursor.lastrowid)

    async def test_a_long_session_replays_its_newest_turns_in_order(self) -> None:
        ids = [self._add(1, f'm{index:02d}') for index in range(1, 26)]

        messages = await self.repository.list_messages(1, limit=20)

        self.assertEqual(ids[5:], [message['id'] for message in messages])
        self.assertEqual(
            [f'm{index:02d}' for index in range(6, 26)],
            [message['content'] for message in messages],
        )
        self.assertEqual(sorted(m['id'] for m in messages), [m['id'] for m in messages])

    async def test_the_default_window_is_also_the_newest_turns(self) -> None:
        ids = [self._add(1, f'm{index:03d}') for index in range(1, 226)]

        messages = await self.repository.list_messages(1)

        self.assertEqual(200, len(messages))
        self.assertEqual(ids[-200:], [message['id'] for message in messages])
        self.assertEqual(ids[-200], messages[0]['id'])
        self.assertEqual(ids[-1], messages[-1]['id'])

    async def test_another_session_is_never_mixed_in(self) -> None:
        mine = [self._add(1, f'mine{index}') for index in range(3)]
        theirs = [self._add(2, f'theirs{index}') for index in range(25)]

        short = await self.repository.list_messages(1)
        long = await self.repository.list_messages(2, limit=20)

        self.assertEqual(mine, [message['id'] for message in short])
        self.assertEqual(theirs[5:], [message['id'] for message in long])
        self.assertFalse(set(mine) & {message['id'] for message in long})

    async def test_a_window_of_one_is_the_latest_turn(self) -> None:
        self._add(1, 'first')
        latest = self._add(1, 'latest')

        messages = await self.repository.list_messages(1, limit=1)

        self.assertEqual([latest], [message['id'] for message in messages])
        self.assertEqual('latest', messages[0]['content'])

    async def test_a_limit_below_one_still_returns_the_latest_turn(self) -> None:
        self._add(1, 'first')
        latest = self._add(1, 'latest')

        for limit in (0, -5):
            with self.subTest(limit=limit):
                messages = await self.repository.list_messages(1, limit=limit)
                self.assertEqual([latest], [message['id'] for message in messages])

    async def test_a_short_history_comes_back_oldest_first_and_unchanged(self) -> None:
        ids = [self._add(1, f'm{index}') for index in range(3)]

        messages = await self.repository.list_messages(1, limit=20)

        self.assertEqual(ids, [message['id'] for message in messages])

    async def test_an_empty_history_is_empty(self) -> None:
        self._add(2, 'elsewhere')

        self.assertEqual([], await self.repository.list_messages(1))

    async def test_rows_keep_their_shape_and_serialize_created_at(self) -> None:
        self._add(1, 'hello')

        [message] = await self.repository.list_messages(1)

        self.assertEqual(
            {'id', 'session_id', 'role', 'content', 'created_at'},
            set(message),
        )
        self.assertEqual(1, message['session_id'])
        self.assertEqual(('user', 'hello'), (message['role'], message['content']))
        self.assertEqual(
            (_START + timedelta(seconds=1)).isoformat(),
            message['created_at'],
        )


if __name__ == '__main__':
    unittest.main()
