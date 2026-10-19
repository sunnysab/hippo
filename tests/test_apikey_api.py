"""API keys: the security rules, which are the part that matters.

A key is a read credential for content. These tests pin what it cannot do — reach
the admin API, mint another key, or write anything — because those are the mistakes
that would turn a convenience feature into a hole.
"""

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import ValidationError

from hippo.api.deps import API_KEY_FORBIDDEN_PREFIXES, api_key_user
from hippo.api.routers import apikey as apikey_api
from hippo.api.schemas.auth import ApiKeyCreateRequest
from hippo.exceptions import ApiError
from hippo.repositories.apikey import generate_key, key_hash, key_prefix


def _request(*, path='/api/article', method='GET', headers=None) -> SimpleNamespace:
    return SimpleNamespace(
        headers=headers or {},
        method=method,
        url=SimpleNamespace(path=path),
        state=SimpleNamespace(),
        client=SimpleNamespace(host='127.0.0.1'),
    )


def _storage(*, key: dict | None = None, user: SimpleNamespace | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        api_keys=AsyncMock(
            authenticate=AsyncMock(return_value=key),
            touch=AsyncMock(),
        ),
        users=SimpleNamespace(get=AsyncMock(return_value=user)),
        commit=AsyncMock(),
    )


def _key(scopes=('read',), user_id=7) -> dict:
    return {'id': 1, 'user_id': user_id, 'name': 'CI', 'scopes': list(scopes), 'expires_at': None}


class KeyFormatTest(unittest.TestCase):
    def test_a_key_is_recognisable_and_prefixable(self) -> None:
        secret = generate_key()

        self.assertTrue(secret.startswith('hippo_'))
        self.assertEqual(secret[6:14], key_prefix(secret))
        self.assertNotEqual(secret, generate_key())

    def test_the_hash_is_stable_and_not_the_secret(self) -> None:
        secret = generate_key()

        self.assertEqual(key_hash(secret), key_hash(secret))
        self.assertNotIn(secret, key_hash(secret))


class ApiKeyAuthTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_header_is_not_a_key(self) -> None:
        self.assertIsNone(await api_key_user(_request(), _storage()))

    async def test_a_bearer_key_resolves_to_its_user(self) -> None:
        user = SimpleNamespace(id=7, username='u', is_disabled=False)
        storage = _storage(key=_key(), user=user)
        request = _request(headers={'authorization': 'Bearer hippo_abc'})

        resolved = await api_key_user(request, storage)

        self.assertIs(user, resolved)
        storage.api_keys.authenticate.assert_awaited_with('hippo_abc')
        storage.api_keys.touch.assert_awaited()
        self.assertEqual([{'id': 1, 'name': 'CI', 'scopes': ['read']}], [request.state.api_key])

    async def test_the_x_api_key_header_also_works(self) -> None:
        user = SimpleNamespace(id=7, username='u', is_disabled=False)
        storage = _storage(key=_key(), user=user)

        resolved = await api_key_user(_request(headers={'x-api-key': 'hippo_abc'}), storage)

        self.assertIs(user, resolved)

    async def test_an_unknown_key_is_unauthorised(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            await api_key_user(_request(headers={'x-api-key': 'hippo_nope'}), _storage(key=None))

        self.assertEqual(401, ctx.exception.status)

    async def test_a_disabled_account_cannot_use_its_key(self) -> None:
        user = SimpleNamespace(id=7, username='u', is_disabled=True)
        storage = _storage(key=_key(), user=user)

        with self.assertRaises(ApiError) as ctx:
            await api_key_user(_request(headers={'x-api-key': 'hippo_abc'}), storage)

        self.assertEqual(403, ctx.exception.status)

    async def test_a_read_only_key_cannot_write(self) -> None:
        storage = _storage(key=_key(scopes=('read',)), user=SimpleNamespace(id=7, is_disabled=False))

        with self.assertRaises(ApiError) as ctx:
            await api_key_user(_request(method='POST', headers={'x-api-key': 'hippo_abc'}), storage)

        self.assertEqual(403, ctx.exception.status)
        storage.api_keys.touch.assert_not_awaited()

    async def test_a_write_key_may_write(self) -> None:
        storage = _storage(key=_key(scopes=('read', 'write')), user=SimpleNamespace(id=7, is_disabled=False))

        resolved = await api_key_user(_request(method='POST', headers={'x-api-key': 'hippo_abc'}), storage)

        self.assertIsNotNone(resolved)

    async def test_a_key_cannot_reach_the_admin_api(self) -> None:
        storage = _storage(key=_key(), user=SimpleNamespace(id=7, is_disabled=False))

        with self.assertRaises(ApiError) as ctx:
            await api_key_user(_request(path='/api/admin/site', headers={'x-api-key': 'hippo_abc'}), storage)

        self.assertEqual(403, ctx.exception.status)
        # 连查库都不该发生：路径检查在认证之前
        storage.api_keys.authenticate.assert_not_awaited()

    async def test_every_forbidden_prefix_is_actually_forbidden(self) -> None:
        for prefix in API_KEY_FORBIDDEN_PREFIXES:
            with self.subTest(prefix=prefix), self.assertRaises(ApiError):
                await api_key_user(
                    _request(path=f'{prefix}/x', headers={'x-api-key': 'hippo_abc'}),
                    _storage(key=_key(), user=SimpleNamespace(id=7, is_disabled=False)),
                )


class RouterTest(unittest.IsolatedAsyncioTestCase):
    def _user(self) -> SimpleNamespace:
        return SimpleNamespace(id=1, username='u', role='admin')

    async def test_creating_a_key_returns_the_secret_once(self) -> None:
        storage = SimpleNamespace(
            transaction=_NullTransaction,
            api_keys=AsyncMock(),
            audit=AsyncMock(),
        )
        storage.api_keys.create = AsyncMock(
            return_value=(
                {'id': 5, 'name': 'CI', 'prefix': 'abcd1234', 'scopes': ['read'], 'created_at': None},
                'hippo_secret',
            )
        )

        result = await apikey_api.create_api_key(
            request=SimpleNamespace(headers={}, client=None),
            body=ApiKeyCreateRequest(name='CI'),
            user=self._user(),
            storage=storage,
        )

        self.assertEqual('hippo_secret', result['secret'])
        self.assertEqual(['read'], result['scopes'])
        storage.audit.record.assert_awaited()

    async def test_listing_never_includes_a_secret(self) -> None:
        storage = SimpleNamespace(
            transaction=_NullTransaction,
            api_keys=AsyncMock(),
            audit=AsyncMock(),
        )
        storage.api_keys.list_for_user = AsyncMock(
            return_value=[{'id': 5, 'name': 'CI', 'prefix': 'abcd1234', 'scopes': ['read']}]
        )

        result = await apikey_api.list_api_keys(user=self._user(), storage=storage)

        self.assertNotIn('secret', str(result))
        self.assertEqual(['read', 'write'], result['scopes'])

    async def test_an_unknown_scope_is_ignored(self) -> None:
        storage = SimpleNamespace(transaction=_NullTransaction, api_keys=AsyncMock(), audit=AsyncMock())
        storage.api_keys.create = AsyncMock(
            return_value=({'id': 5, 'name': 'CI', 'prefix': 'abcd1234', 'scopes': ['read']}, 'hippo_secret')
        )

        await apikey_api.create_api_key(
            request=SimpleNamespace(headers={}, client=None),
            body=ApiKeyCreateRequest(scopes=['admin', 'read']),
            user=self._user(),
            storage=storage,
        )

        self.assertEqual(('read',), storage.api_keys.create.call_args.kwargs['scopes'])

    async def test_an_expiry_is_computed_in_days(self) -> None:
        storage = SimpleNamespace(transaction=_NullTransaction, api_keys=AsyncMock(), audit=AsyncMock())
        storage.api_keys.create = AsyncMock(
            return_value=({'id': 5, 'name': 'CI', 'prefix': 'abcd1234', 'scopes': ['read']}, 'hippo_secret')
        )

        before = datetime.now(UTC)
        await apikey_api.create_api_key(
            request=SimpleNamespace(headers={}, client=None),
            body=ApiKeyCreateRequest(expires_in_days=30),
            user=self._user(),
            storage=storage,
        )

        expires_at = storage.api_keys.create.call_args.kwargs['expires_at']
        self.assertIsNotNone(expires_at)
        self.assertLess(abs((expires_at - (before + timedelta(days=30))).total_seconds()), 5)

    async def test_a_nonsense_expiry_is_refused_by_the_request_model(self) -> None:
        # The body is declared as an integer number of days, so a string is
        # refused before the route runs. _expiry only has to explain 0 or less.
        with self.assertRaises(ValidationError):
            ApiKeyCreateRequest(expires_in_days='soon')  # type: ignore[arg-type]
        storage = SimpleNamespace(transaction=_NullTransaction, api_keys=AsyncMock(), audit=AsyncMock())
        with self.assertRaises(ApiError):
            await apikey_api.create_api_key(
                request=SimpleNamespace(headers={}, client=None),
                body=ApiKeyCreateRequest(expires_in_days=0),
                user=self._user(),
                storage=storage,
            )

    async def test_revoking_someone_elses_key_is_a_404(self) -> None:
        storage = SimpleNamespace(transaction=_NullTransaction, api_keys=AsyncMock(), audit=AsyncMock())
        storage.api_keys.revoke = AsyncMock(return_value=False)

        with self.assertRaises(ApiError) as ctx:
            await apikey_api.revoke_api_key(
                key_id=9,
                request=SimpleNamespace(headers={}, client=None),
                user=self._user(),
                storage=storage,
            )

        self.assertEqual(404, ctx.exception.status)

    async def test_whoami_reports_how_it_authenticated(self) -> None:
        request = _request()
        request.state.api_key = {'id': 3, 'name': 'CI', 'scopes': ['read']}

        by_key = await apikey_api.whoami(request=request, user=self._user())
        by_session = await apikey_api.whoami(request=_request(), user=self._user())

        self.assertEqual('api_key', by_key['via'])
        self.assertEqual(['read'], by_key['scopes'])
        self.assertEqual(3, by_key['key']['id'])
        self.assertEqual('session', by_session['via'])
        self.assertIsNone(by_session['key'])


class _NullTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


if __name__ == '__main__':
    unittest.main()
