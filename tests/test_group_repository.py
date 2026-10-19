import unittest
from types import SimpleNamespace

from hippo.repositories.account import GroupRepository


class _Cursor:
    def __init__(self, executed: list[tuple[str, list]]) -> None:
        self._executed = executed

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql: str, params=None) -> None:
        self._executed.append((' '.join(sql.split()), list(params or [])))

    async def fetchall(self) -> list[dict]:
        return []


class GroupRepositoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_source_narrows_the_count_without_dropping_groups(self) -> None:
        # 来源过滤必须挂在 join 上：写进 WHERE 会把没有该来源账号的分组整行吞掉。
        executed: list[tuple[str, list]] = []
        repository = GroupRepository(SimpleNamespace(cursor=lambda **kwargs: _Cursor(executed)))

        await repository.list_groups(user_id=7, source='bilibili')

        sql, params = executed[0]
        self.assertIn('LEFT JOIN accounts a ON a.biz = s.biz AND a.source = %s', sql)
        self.assertEqual([7, 'bilibili', 7], params)

    async def test_without_a_source_every_channel_counts(self) -> None:
        executed: list[tuple[str, list]] = []
        repository = GroupRepository(SimpleNamespace(cursor=lambda **kwargs: _Cursor(executed)))

        await repository.list_groups(user_id=7)

        sql, params = executed[0]
        self.assertNotIn('AND a.source = %s', sql)
        self.assertEqual([7, 7], params)


if __name__ == '__main__':
    unittest.main()
