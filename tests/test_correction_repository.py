from unittest.mock import AsyncMock

import pytest

from hippo.repositories.correction import CorrectionRepository


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Cursor:
    def __init__(self, rows=()):
        self.rows = rows
        self.rowcount = 0
        self.execute = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return None


class Connection:
    def __init__(self, cursor):
        self.cur = cursor

    def cursor(self, **kwargs):
        return self.cur


@pytest.mark.anyio
async def test_repository_ownership_and_scope_predicates():
    cursor = Cursor([('one',)])
    repo = CorrectionRepository(Connection(cursor))
    assert not await repo.valid_scope(7, ['one', 'other'])
    sql, params = cursor.execute.await_args.args
    assert 'user_id = %s' in sql and 'NOT is_disabled' in sql and 'FOR SHARE' in sql
    assert params == (7, ['one', 'other'])
    cursor.rows = []
    await repo.list_for_user(7)
    sql, params = cursor.execute.await_args.args
    assert 'user_id = %s' in sql and params == (7,)
    assert not await repo.delete(7, 99)
    sql, params = cursor.execute.await_args.args
    assert 'user_id = %s' in sql and params == (7, 99)
    assert await repo.update(7, 99, dict(source_text='a', replacement_text='b', account_biz=[], enabled=True)) is None
    sql, params = cursor.execute.await_args.args
    assert 'user_id = %s' in sql and params[-2:] == (7, 99)
