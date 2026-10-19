"""Login gating and the self-service e-mail change.

Two rules matter: an account with no address is never blocked by e-mail
verification, and an authenticated address change must not lock its owner out.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hippo.api.routers import auth as auth_api
from hippo.api.schemas.auth import EmailChangeRequest, LoginRequest
from hippo.exceptions import ApiError
from hippo.security import hash_password


def _request(host: str = '127.0.0.1') -> SimpleNamespace:
    return SimpleNamespace(headers={'user-agent': 'test'}, client=SimpleNamespace(host=host))


class _NullTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _user(user_id: int = 1, *, email: str | None = None, verified: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        username='u',
        email=email,
        email_verified=verified,
        is_disabled=False,
        role='user',
    )


def _storage(user: SimpleNamespace | None, password_hash: str | None, *, taken=None) -> SimpleNamespace:
    return SimpleNamespace(
        transaction=lambda: _NullTransaction(),
        users=SimpleNamespace(
            get_with_password=AsyncMock(return_value=(user, password_hash) if user else None),
            get_by_email=AsyncMock(return_value=taken),
            set_email=AsyncMock(),
        ),
        sessions=SimpleNamespace(issue=AsyncMock(return_value='token')),
        audit=SimpleNamespace(record=AsyncMock()),
    )


class LoginVerificationGateTest(unittest.IsolatedAsyncioTestCase):
    async def test_an_account_without_an_email_can_log_in_unverified(self) -> None:
        user = _user(email=None, verified=False)
        storage = _storage(user, hash_password('correct-horse'))

        response = await auth_api.login(
            _request('10.0.0.1'),
            body=LoginRequest(username='u', password='correct-horse'),
            storage=storage,
        )

        self.assertEqual(204, response.status_code)

    async def test_an_unverified_address_still_blocks_login(self) -> None:
        user = _user(email='u@example.com', verified=False)
        storage = _storage(user, hash_password('correct-horse'))

        with self.assertRaises(ApiError) as ctx:
            await auth_api.login(
                _request('10.0.0.2'),
                body=LoginRequest(username='u', password='correct-horse'),
                storage=storage,
            )

        self.assertEqual(403, ctx.exception.status)


class ChangeEmailTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_new_address_is_stored_as_verified(self) -> None:
        user = _user(email=None)
        storage = _storage(user, hash_password('correct-horse'))

        await auth_api.change_email(
            _request(),
            body=EmailChangeRequest(current_password='correct-horse', email='New@Example.com'),
            user=user,
            storage=storage,
        )

        storage.users.set_email.assert_awaited_once_with(1, 'New@Example.com', verified=True)
        self.assertEqual('auth.email_changed', storage.audit.record.await_args.args[1])

    async def test_clearing_the_address_unverifies_it(self) -> None:
        user = _user(email='old@example.com', verified=True)
        storage = _storage(user, hash_password('correct-horse'))

        await auth_api.change_email(
            _request(),
            body=EmailChangeRequest(current_password='correct-horse', email=''),
            user=user,
            storage=storage,
        )

        storage.users.set_email.assert_awaited_once_with(1, None, verified=False)

    async def test_a_wrong_password_changes_nothing(self) -> None:
        user = _user(email=None)
        storage = _storage(user, hash_password('correct-horse'))

        with self.assertRaises(ApiError) as ctx:
            await auth_api.change_email(
                _request(),
                body=EmailChangeRequest(current_password='wrong', email='new@example.com'),
                user=user,
                storage=storage,
            )

        self.assertEqual(400, ctx.exception.status)
        storage.users.set_email.assert_not_awaited()

    async def test_an_address_owned_by_someone_else_is_refused(self) -> None:
        user = _user(user_id=1, email=None)
        storage = _storage(user, hash_password('correct-horse'), taken=_user(user_id=2, email='taken@example.com'))

        with self.assertRaises(ApiError) as ctx:
            await auth_api.change_email(
                _request(),
                body=EmailChangeRequest(current_password='correct-horse', email='taken@example.com'),
                user=user,
                storage=storage,
            )

        self.assertEqual(409, ctx.exception.status)


if __name__ == '__main__':
    unittest.main()
