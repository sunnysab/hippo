import asyncio
import unittest


class DefaultProviderTest(unittest.TestCase):
    """The default provider is what chat resolves to, so only a chat one may be it."""

    def setUp(self):
        from hippo.repositories.llm import LlmProviderRepository

        self.repo = LlmProviderRepository.__new__(LlmProviderRepository)
        self.calls: list[tuple[str, object]] = []

    def _wire(self, existing: int, returned: dict) -> None:
        async def count():
            self.calls.append(('count', None))
            return existing

        async def set_default(provider_id):
            self.calls.append(('set_default', provider_id))

        async def get(provider_id):
            return returned

        self.repo.count = count
        self.repo.set_default = set_default
        self.repo.get = get

        class _Cursor:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, *args, **kwargs):
                return None

            async def fetchone(self):
                return returned

        self.repo._conn = type('C', (), {'cursor': lambda *a, **k: _Cursor()})()

    def test_the_first_chat_provider_becomes_the_default(self):
        self._wire(0, {'id': 1, 'kind': 'chat'})
        asyncio.run(self.repo.create(name='a', base_url='u', api_key='k', model='m'))
        self.assertIn(('set_default', 1), self.calls)

    def test_a_decision_provider_never_becomes_the_default(self):
        # A table holding only a decision provider must stay without a default,
        # or every chat request resolves to a model that cannot chat.
        self._wire(0, {'id': 1, 'kind': 'decision'})
        asyncio.run(self.repo.create(name='c', base_url='u', api_key='', model='clef-flash', kind='decision'))
        self.assertNotIn(('set_default', 1), self.calls)

    def test_an_existing_provider_does_not_make_a_second_one_default(self):
        self._wire(3, {'id': 9, 'kind': 'chat'})
        asyncio.run(self.repo.create(name='b', base_url='u', api_key='k', model='m'))
        self.assertNotIn(('set_default', 9), self.calls)
