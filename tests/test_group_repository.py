import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    async def test_group_counts_keep_enabled_total_and_paused_subscriptions_separate(self) -> None:
        row = {'id': 1, 'name': 'Default', 'account_count': 77, 'subscription_count': 100, 'disabled_count': 23}
        cursor = AsyncMock()
        cursor.__aenter__.return_value = cursor
        cursor.fetchone.return_value = row
        cursor.fetchall.return_value = [row]
        repository = GroupRepository(SimpleNamespace(cursor=lambda **kwargs: cursor))

        detail = await repository.get_group(1, user_id=7)
        groups = await repository.list_groups(user_id=7)

        for group in [detail, *groups]:
            self.assertEqual((77, 100, 23), (group.account_count, group.subscription_count, group.disabled_count))
        for call in cursor.execute.await_args_list:
            sql = ' '.join(call.args[0].split())
            self.assertIn('FILTER (WHERE NOT s.is_disabled) AS account_count', sql)
            self.assertIn('FILTER (WHERE s.is_disabled) AS disabled_count', sql)

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
