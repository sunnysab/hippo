"""LLM provider admin behaviour.

Two things must never regress: the stored API key never leaves the process in
clear, and an empty key in an update means "keep the old one" rather than
"erase it".
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hippo.api.routers import llm as llm_api
from hippo.exceptions import ApiError
from hippo.repositories.llm import mask_key


class _NullTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class MaskKeyTest(unittest.TestCase):
    def test_only_the_last_four_characters_survive(self) -> None:
        masked = mask_key('sk-1234567890abcdef')
        self.assertTrue(masked.endswith('cdef'))
        self.assertNotIn('1234567890ab', masked)

    def test_short_keys_are_fully_hidden(self) -> None:
        self.assertEqual('•••', mask_key('abc'))

    def test_empty_key_masks_to_empty(self) -> None:
        self.assertEqual('', mask_key(''))


class ProviderUpdateTest(unittest.IsolatedAsyncioTestCase):
    async def test_blank_api_key_leaves_the_stored_one_untouched(self) -> None:
        updated = {'name': 'p', 'base_url': 'http://x', 'model': 'm'}
        storage = SimpleNamespace(
            transaction=lambda: _NullTransaction(),
            llm=SimpleNamespace(
                get=AsyncMock(return_value={'id': 1, 'name': 'p'}),
                update=AsyncMock(return_value=updated),
            ),
            audit=SimpleNamespace(record=AsyncMock()),
        )

        await llm_api.update_provider(
            '1',
            SimpleNamespace(headers={}, client=None),
            body={'name': 'renamed', 'api_key': ''},
            actor=SimpleNamespace(id=1, username='admin'),
            storage=storage,
        )

        self.assertIsNone(storage.llm.update.await_args.kwargs['api_key'])

    async def test_a_new_api_key_is_passed_through(self) -> None:
        storage = SimpleNamespace(
            transaction=lambda: _NullTransaction(),
            llm=SimpleNamespace(
                get=AsyncMock(return_value={'id': 1, 'name': 'p'}),
                update=AsyncMock(return_value={'name': 'p'}),
            ),
            audit=SimpleNamespace(record=AsyncMock()),
        )

        await llm_api.update_provider(
            '1',
            SimpleNamespace(headers={}, client=None),
            body={'api_key': 'sk-new'},
            actor=SimpleNamespace(id=1, username='admin'),
            storage=storage,
        )

        self.assertEqual('sk-new', storage.llm.update.await_args.kwargs['api_key'])

    async def test_create_requires_every_field(self) -> None:
        storage = SimpleNamespace(
            llm=SimpleNamespace(create=AsyncMock()),
            audit=SimpleNamespace(record=AsyncMock()),
        )
        with self.assertRaises(ApiError):
            await llm_api.create_provider(
                SimpleNamespace(headers={}, client=None),
                body={'name': 'p', 'base_url': 'http://x', 'model': 'm'},
                actor=SimpleNamespace(id=1, username='admin'),
                storage=storage,
            )
        storage.llm.create.assert_not_awaited()

    async def test_base_url_is_normalized(self) -> None:
        storage = SimpleNamespace(
            transaction=lambda: _NullTransaction(),
            llm=SimpleNamespace(create=AsyncMock(return_value={'id': 1, 'name': 'p'})),
            audit=SimpleNamespace(record=AsyncMock()),
        )
        await llm_api.create_provider(
            SimpleNamespace(headers={}, client=None),
            body={
                'name': 'p',
                'base_url': 'http://localhost:8000/v1/',
                'api_key': 'k',
                'model': 'm',
            },
            actor=SimpleNamespace(id=1, username='admin'),
            storage=storage,
        )
        self.assertEqual('http://localhost:8000/v1', storage.llm.create.await_args.kwargs['base_url'])


if __name__ == '__main__':
    unittest.main()
