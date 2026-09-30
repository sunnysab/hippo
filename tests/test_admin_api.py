"""Admin user-management guards.

The interesting behaviour is what must be refused: an admin locking themselves
out, and silent password/session changes going unlogged.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.api.routers import admin as admin_api
from hippo.exceptions import ApiError


def _user(user_id: int = 1, role: str = 'admin') -> SimpleNamespace:
    return SimpleNamespace(id=user_id, username=f'u{user_id}', role=role)


def _storage(*, target: SimpleNamespace | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        users=SimpleNamespace(
            get=AsyncMock(return_value=target),
            set_disabled=AsyncMock(),
            set_role=AsyncMock(),
            set_password=AsyncMock(),
        ),
        sessions=SimpleNamespace(revoke_all_for_user=AsyncMock(return_value=2)),
        audit=SimpleNamespace(record=AsyncMock()),
    )


class AdminGuardTest(unittest.IsolatedAsyncioTestCase):
    async def test_an_admin_cannot_disable_themselves(self) -> None:
        storage = _storage(target=_user(1))
        with self.assertRaises(ApiError) as ctx:
            await admin_api.update_user(
                '1',
                SimpleNamespace(headers={}, client=SimpleNamespace(host='127.0.0.1')),
                body={'is_disabled': True},
                actor=_user(1),
                storage=storage,
            )
        self.assertEqual(400, ctx.exception.status)
        storage.users.set_disabled.assert_not_awaited()

    async def test_an_admin_cannot_demote_themselves(self) -> None:
        storage = _storage(target=_user(1))
        with self.assertRaises(ApiError):
            await admin_api.update_user(
                '1',
                SimpleNamespace(headers={}, client=SimpleNamespace(host='127.0.0.1')),
                body={'role': 'user'},
                actor=_user(1),
                storage=storage,
            )
        storage.users.set_role.assert_not_awaited()

    async def test_disabling_a_user_revokes_their_sessions(self) -> None:
        storage = _storage(target=_user(7, role='user'))
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host='10.0.0.1'))

        await admin_api.update_user(
            '7', request, body={'is_disabled': True}, actor=_user(1), storage=storage
        )

        storage.sessions.revoke_all_for_user.assert_awaited_once_with(7)
        action = storage.audit.record.await_args.args[1]
        self.assertEqual('admin.user_updated', action)

    async def test_reset_password_revokes_sessions_and_audits(self) -> None:
        storage = _storage(target=_user(7, role='user'))
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host='10.0.0.1'))

        await admin_api.reset_password(
            '7', request, body={'password': 'a-good-password'}, actor=_user(1), storage=storage
        )

        storage.users.set_password.assert_awaited_once()
        storage.sessions.revoke_all_for_user.assert_awaited_once_with(7)
        self.assertEqual('admin.password_reset', storage.audit.record.await_args.args[1])

    async def test_short_passwords_are_rejected(self) -> None:
        storage = _storage(target=_user(7, role='user'))
        with self.assertRaises(ApiError):
            await admin_api.reset_password(
                '7',
                SimpleNamespace(headers={}, client=None),
                body={'password': 'short'},
                actor=_user(1),
                storage=storage,
            )
        storage.users.set_password.assert_not_awaited()

    async def test_toggling_registration_is_audited(self) -> None:
        storage = _storage()
        storage.site = None
        settings = {'registration_enabled': True, 'site_name': 'Hippo', 'public_base_url': ''}
        before = {**settings, 'registration_enabled': False}

        with patch.object(admin_api, 'get_site_settings', AsyncMock(return_value=before)), patch.object(
            admin_api, 'set_site_settings', AsyncMock(return_value=settings)
        ):
            result = await admin_api.update_site_settings(
                SimpleNamespace(headers={}, client=None),
                body={'registration_enabled': True},
                actor=_user(1),
                storage=storage,
            )

        self.assertTrue(result['registration_enabled'])
        self.assertEqual('admin.site_settings_updated', storage.audit.record.await_args.args[1])

    async def test_an_unchanged_value_is_not_audited(self) -> None:
        storage = _storage()
        settings = {'registration_enabled': False, 'site_name': 'Hippo', 'public_base_url': ''}

        with patch.object(admin_api, 'get_site_settings', AsyncMock(return_value=dict(settings))), patch.object(
            admin_api, 'set_site_settings', AsyncMock(return_value=settings)
        ):
            await admin_api.update_site_settings(
                SimpleNamespace(headers={}, client=None),
                body={'site_name': 'Hippo'},
                actor=_user(1),
                storage=storage,
            )

        storage.audit.record.assert_not_awaited()

    async def test_unknown_role_is_rejected(self) -> None:
        storage = _storage(target=_user(7, role='user'))
        with self.assertRaises(ApiError):
            await admin_api.update_user(
                '7',
                SimpleNamespace(headers={}, client=None),
                body={'role': 'superuser'},
                actor=_user(1),
                storage=storage,
            )


if __name__ == '__main__':
    unittest.main()
