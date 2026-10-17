"""Operator entry point: schema init and account bootstrap.

Everything here runs against a fake storage: the point is the contract the
commands expose (exit codes, duplicate handling, password floor, pool cleanup),
not psycopg itself.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo import ops


class _NullTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _storage(*, existing=None, created=None) -> SimpleNamespace:
    return SimpleNamespace(
        transaction=lambda: _NullTransaction(),
        users=SimpleNamespace(
            get_with_password=AsyncMock(return_value=existing),
            create=AsyncMock(return_value=created or SimpleNamespace(id=3, username='admin', role='admin')),
        ),
        subscriptions=SimpleNamespace(
            claim_orphan_groups=AsyncMock(return_value=2),
            backfill_from_accounts=AsyncMock(return_value=5),
        ),
    )


class _FakeStorageContext:
    """``open_storage()`` returning a preset handle, recording the entry/exit."""

    def __init__(self, storage: SimpleNamespace) -> None:
        self.storage = storage
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> SimpleNamespace:
        self.entered += 1
        return self.storage

    async def __aexit__(self, *exc):
        self.exited += 1
        return False


class InitDbTest(unittest.IsolatedAsyncioTestCase):
    async def test_init_db_uses_auto_init_and_closes_the_pool(self) -> None:
        captured: dict[str, object] = {}
        closed = AsyncMock()

        class DummyStorage:
            def __init__(self, dsn, **kwargs) -> None:
                captured['dsn'] = dsn
                captured['kwargs'] = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        with (
            patch('hippo.ops.PostgresStorage', DummyStorage),
            patch('hippo.ops.close_pool', closed),
        ):
            await ops.init_db(dsn='postgresql://example')

        self.assertEqual('postgresql://example', captured['dsn'])
        self.assertTrue(captured['kwargs']['auto_init'])
        # A one-shot command must not leave the pool attached to a dead loop.
        closed.assert_awaited_once()

    async def test_init_db_without_dsn_reports_the_missing_setting(self) -> None:
        with (
            patch('hippo.ops.get_settings') as get_settings,
            self.assertRaisesRegex(ops.StorageInitError, r'Missing PostgreSQL DSN'),
        ):
            get_settings.return_value = SimpleNamespace(database=SimpleNamespace(dsn=''))
            await ops.init_db(dsn=None)


class CreateAdminTest(unittest.IsolatedAsyncioTestCase):
    async def test_creates_a_verified_admin(self) -> None:
        storage = _storage()
        context = _FakeStorageContext(storage)
        closed = AsyncMock()

        with (
            patch('hippo.ops.open_storage', return_value=context),
            patch('hippo.ops.close_pool', closed),
            patch('hippo.ops.hash_password', return_value='hashed') as hasher,
        ):
            code = await ops.create_admin(username='admin', password='a-good-password', email='a@b.c')

        self.assertEqual(0, code)
        self.assertEqual(1, context.entered)
        kwargs = storage.users.create.await_args.kwargs
        self.assertEqual('admin', kwargs['username'])
        self.assertEqual('hashed', kwargs['password_hash'])
        self.assertEqual('admin', kwargs['role'])
        self.assertTrue(kwargs['email_verified'])
        self.assertEqual('a@b.c', kwargs['email'])
        hasher.assert_called_once_with('a-good-password')
        closed.assert_awaited_once()

    async def test_duplicate_user_fails_without_writing(self) -> None:
        existing = (SimpleNamespace(id=1, username='admin', role='admin'), 'old-hash')
        storage = _storage(existing=existing)
        context = _FakeStorageContext(storage)

        with (
            patch('hippo.ops.open_storage', return_value=context),
            patch('hippo.ops.close_pool', AsyncMock()),
            patch('hippo.ops.hash_password') as hasher,
        ):
            code = await ops.create_admin(username='admin', password='a-good-password')

        self.assertEqual(1, code)
        hasher.assert_not_called()
        storage.users.create.assert_not_awaited()

    async def test_pool_is_closed_even_when_creation_raises(self) -> None:
        storage = _storage()
        storage.users.create = AsyncMock(side_effect=RuntimeError('boom'))
        closed = AsyncMock()

        with (
            patch('hippo.ops.open_storage', return_value=_FakeStorageContext(storage)),
            patch('hippo.ops.close_pool', closed),
            self.assertRaises(RuntimeError),
        ):
            await ops.create_admin(username='admin', password='a-good-password')

        closed.assert_awaited_once()


class AssignOwnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_assigns_orphan_rows_to_an_existing_user(self) -> None:
        owner = SimpleNamespace(id=9, username='alice')
        storage = _storage(existing=(owner, 'hash'))
        closed = AsyncMock()

        with (
            patch('hippo.ops.open_storage', return_value=_FakeStorageContext(storage)),
            patch('hippo.ops.close_pool', closed),
        ):
            code = await ops.assign_owner(owner='alice')

        self.assertEqual(0, code)
        storage.subscriptions.claim_orphan_groups.assert_awaited_once_with(9)
        storage.subscriptions.backfill_from_accounts.assert_awaited_once_with(9)
        closed.assert_awaited_once()

    async def test_unknown_owner_fails_without_writing(self) -> None:
        storage = _storage(existing=None)

        with (
            patch('hippo.ops.open_storage', return_value=_FakeStorageContext(storage)),
            patch('hippo.ops.close_pool', AsyncMock()),
        ):
            code = await ops.assign_owner(owner='nobody')

        self.assertEqual(1, code)
        storage.subscriptions.claim_orphan_groups.assert_not_awaited()


class ParserTest(unittest.TestCase):
    def test_help_does_not_touch_settings_or_the_database(self) -> None:
        with (
            patch('hippo.ops.get_settings') as get_settings,
            patch('hippo.ops.open_storage') as open_storage,
            patch('hippo.ops.PostgresStorage') as postgres,
            self.assertRaises(SystemExit) as ctx,
        ):
            ops.main(['--help'])

        self.assertEqual(0, ctx.exception.code)
        get_settings.assert_not_called()
        open_storage.assert_not_called()
        postgres.assert_not_called()

    def test_invalid_arguments_do_not_touch_settings(self) -> None:
        with (
            patch('hippo.ops.get_settings') as get_settings,
            patch('hippo.ops.open_storage') as open_storage,
            self.assertRaises(SystemExit) as ctx,
        ):
            ops.main([])

        self.assertEqual(2, ctx.exception.code)
        get_settings.assert_not_called()
        open_storage.assert_not_called()

    def test_create_admin_rejects_a_short_password(self) -> None:
        with patch('hippo.ops.open_storage') as open_storage:
            code = ops.main(['create-admin', '--username', 'admin', '--password', 'short'])

        self.assertEqual(2, code)
        open_storage.assert_not_called()

    def test_create_admin_rejects_an_empty_username(self) -> None:
        with patch('hippo.ops.open_storage') as open_storage:
            code = ops.main(['create-admin', '--username', '  ', '--password', 'a-good-password'])

        self.assertEqual(2, code)
        open_storage.assert_not_called()

    def test_prompt_rejects_a_mismatched_password(self) -> None:
        with (
            patch('hippo.ops.getpass.getpass', side_effect=['a-good-password', 'different-password']),
            patch('hippo.ops.open_storage') as open_storage,
        ):
            code = ops.main(['create-admin', '--username', 'admin'])

        self.assertEqual(2, code)
        open_storage.assert_not_called()

    def test_missing_dsn_is_reported_as_an_operational_failure(self) -> None:
        with (
            patch('hippo.ops.getpass.getpass', side_effect=['a-good-password', 'a-good-password']) as prompt,
            patch('hippo.ops.open_storage', side_effect=ops.StorageInitError('no dsn')),
        ):
            code = ops.main(['create-admin', '--username', 'admin'])

        self.assertEqual(2, code)
        self.assertEqual(2, prompt.call_count)


if __name__ == '__main__':
    unittest.main()
