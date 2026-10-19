"""Which cookie a shared media account is synced with.

The catalogue is shared and a cookie is personal, so the order is fixed: the
followers' own first, the instance's last. Only a credential refusal moves on --
retrying a 404 with somebody else's cookie would multiply the requests for
nothing, which is what these tests pin.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hippo.sources import CredentialRejected
from hippo.sources.chain import credentials_for, run_with_fallback
from hippo.sources.ingest import MediaResolverRegistry


def _storage(secrets: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        credentials=SimpleNamespace(for_biz=AsyncMock(return_value=secrets)),
    )


class _Template:
    """Stands in for a source that can be rebuilt with a different cookie."""

    def __init__(self, secret: str = 'instance') -> None:
        self.secret = secret
        self.rebuilt: list[str] = []

    def with_credential(self, secret: str) -> _Template:
        candidate = _Template(secret)
        candidate.rebuilt = [*self.rebuilt, secret]
        return candidate


class CredentialsForTest(unittest.IsolatedAsyncioTestCase):
    async def test_followers_come_before_the_instance_credential(self) -> None:
        storage = _storage(['mine', 'theirs'])

        chain = await credentials_for(storage, biz='bilibili:1', source='bilibili', instance='platform')

        self.assertEqual(('mine', 'theirs', 'platform'), chain)
        storage.credentials.for_biz.assert_awaited_once_with('bilibili:1', 'bilibili')

    async def test_a_follower_using_the_instance_cookie_does_not_duplicate_it(self) -> None:
        storage = _storage(['platform'])

        chain = await credentials_for(storage, biz='bilibili:1', source='bilibili', instance='platform')

        self.assertEqual(('platform',), chain)

    async def test_an_unsubscribed_account_only_has_the_instance_credential(self) -> None:
        storage = _storage([])

        chain = await credentials_for(storage, biz='', source='bilibili', instance='platform')

        self.assertEqual(('platform',), chain)
        storage.credentials.for_biz.assert_not_awaited()


class RunWithFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_first_accepted_credential_wins(self) -> None:
        template = _Template()
        seen: list[str] = []

        async def attempt(candidate: _Template) -> str:
            seen.append(candidate.secret)
            return f'listed with {candidate.secret}'

        result = await run_with_fallback(template, ('mine', 'theirs', 'platform'), attempt)

        self.assertEqual('listed with mine', result)
        self.assertEqual(['mine'], seen)

    async def test_a_refusal_moves_on_to_the_next_credential(self) -> None:
        template = _Template()
        seen: list[str] = []

        async def attempt(candidate: _Template) -> str:
            seen.append(candidate.secret)
            if candidate.secret == 'mine':
                raise CredentialRejected('需要有效的 SESSDATA')
            return candidate.secret

        result = await run_with_fallback(template, ('mine', 'theirs'), attempt)

        self.assertEqual('theirs', result)
        self.assertEqual(['mine', 'theirs'], seen)

    async def test_other_failures_are_not_retried(self) -> None:
        template = _Template()
        seen: list[str] = []

        async def attempt(candidate: _Template) -> str:
            seen.append(candidate.secret)
            raise RuntimeError('网络不通')

        with self.assertRaises(RuntimeError):
            await run_with_fallback(template, ('mine', 'theirs', 'platform'), attempt)

        # 换凭证救不回网络错误，只会把请求数乘上订阅者数。
        self.assertEqual(['mine'], seen)

    async def test_every_refusal_raises_the_last_one(self) -> None:
        template = _Template()

        async def attempt(candidate: _Template) -> str:
            raise CredentialRejected(f'{candidate.secret} 不顶用')

        with self.assertRaises(CredentialRejected) as ctx:
            await run_with_fallback(template, ('mine', 'platform'), attempt)

        self.assertIn('platform 不顶用', str(ctx.exception))

    async def test_every_candidate_is_built_from_the_template(self) -> None:
        template = _Template('platform')
        seen: list[_Template] = []

        async def attempt(candidate: _Template) -> _Template:
            seen.append(candidate)
            if candidate.secret != 'platform':
                raise CredentialRejected('换一份')
            return candidate

        result = await run_with_fallback(template, ('mine', 'platform'), attempt)

        # 哪一份是实例凭证由调用方决定，这一层不去猜——猜就会漏掉一个候选。
        self.assertIsNot(template, result)
        self.assertEqual('platform', result.secret)
        self.assertEqual(['mine', 'platform'], [candidate.secret for candidate in seen])


class MediaSyncCredentialTest(unittest.IsolatedAsyncioTestCase):
    async def test_listing_retries_with_the_next_credential(self) -> None:
        from hippo.sources.ingest import Item, Listing, MediaSync

        attempts: list[str] = []

        class _Source:
            source = 'bilibili'

            def __init__(self, secret: str = 'platform') -> None:
                self.secret = secret

            def with_credential(self, secret: str) -> _Source:
                return _Source(secret)

            async def list_items(self, *, source_key: str, limit: int, cursor: str | None = None) -> Listing:
                attempts.append(self.secret)
                if self.secret == 'stale':
                    raise CredentialRejected('B站风控拦截')
                return Listing(
                    items=[Item(article_id='a', title='A', link='https://h/a', media_url='https://h/a.mp3')],
                    is_end=True,
                )

        source = _Source()
        storage = SimpleNamespace(
            # 列表阶段只用到这三样；_store/_enqueue 走的是同一个 storage 接口。
            meta=SimpleNamespace(get=AsyncMock(return_value=None)),
            articles=SimpleNamespace(save_articles=AsyncMock(return_value=1)),
            media_jobs=SimpleNamespace(enqueue_many=AsyncMock(return_value=1)),
            conn=SimpleNamespace(execute=AsyncMock(side_effect=_pks_row)),
            commit=AsyncMock(),
        )

        async def credentials(biz: str) -> tuple[str, ...]:
            return ('stale', 'fresh')

        stats = await MediaSync(
            storage=storage,
            source=source,
            credentials=credentials,
        ).sync_account(biz='bilibili:1', source_key='1')

        self.assertEqual(1, stats.listed)
        self.assertEqual(1, stats.enqueued)
        # 过期的 cookie 被换掉，列表是用第二份跑出来的。
        self.assertEqual(['stale', 'fresh'], attempts)

    async def test_a_listing_without_credentials_hook_uses_the_source_alone(self) -> None:
        from hippo.sources.ingest import Listing, MediaSync

        class _Source:
            source = 'podcast'

            def __init__(self) -> None:
                self.calls = 0

            async def list_items(self, *, source_key: str, limit: int, cursor: str | None = None) -> Listing:
                self.calls += 1
                return Listing(items=[], is_end=True)

        source = _Source()
        storage = SimpleNamespace(
            meta=SimpleNamespace(get=AsyncMock(return_value=None)),
            articles=SimpleNamespace(save_articles=AsyncMock(return_value=0)),
            media_jobs=SimpleNamespace(enqueue_many=AsyncMock(return_value=0)),
            conn=SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(fetchall=AsyncMock(return_value=[])))),
            commit=AsyncMock(),
        )

        await MediaSync(storage=storage, source=source).sync_account(biz='podcast:x', source_key='feed')

        self.assertEqual(1, source.calls)


async def _pks_row(*_args):
    async def fetchall():
        return [('a', 1)]

    return SimpleNamespace(fetchall=fetchall)


class ResolverRegistryCredentialTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_resolver_is_rebuilt_for_each_credential(self) -> None:
        class _Resolver:
            def __init__(self, secret: str = 'platform') -> None:
                self.secret = secret

            def with_credential(self, secret: str) -> _Resolver:
                return _Resolver(secret)

            async def resolve(self, job):
                if self.secret == 'mine':
                    raise CredentialRejected('需要有效的 SESSDATA')
                return f'resolved with {self.secret}'

        seen: list[str] = []

        async def credentials(biz: str, source: str) -> tuple[str, ...]:
            seen.append(f'{biz}/{source}')
            return ('mine', 'theirs')

        registry = MediaResolverRegistry({'bilibili': _Resolver()}, credentials=credentials)

        resolved = await registry.resolve({'source': 'bilibili', 'biz': 'bilibili:1'})

        self.assertEqual('resolved with theirs', resolved)
        self.assertEqual(['bilibili:1/bilibili'], seen)

    async def test_a_resolver_without_the_hook_is_called_directly(self) -> None:
        calls: list[str] = []

        class _Plain:
            async def resolve(self, job):
                calls.append('called')
                return 'ok'

        async def credentials(biz: str, source: str) -> tuple[str, ...]:
            raise AssertionError('不该为没有凭证概念的解析器查凭证')

        registry = MediaResolverRegistry({'podcast': _Plain()}, credentials=credentials)

        self.assertEqual('ok', await registry.resolve({'source': 'podcast'}))
        self.assertEqual(['called'], calls)


if __name__ == '__main__':
    unittest.main()
