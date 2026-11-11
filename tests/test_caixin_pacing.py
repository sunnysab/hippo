"""Caixin limits survive callers, failures and process restarts."""

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

from hippo.sources import SourceError
from hippo.sources.caixin import CaixinRateLimited, CaixinSource
from hippo.sources.caixin_pacing import (
    DAILY_ARTICLES,
    DAILY_OPERATIONS,
    CaixinPacer,
    CaixinPaused,
    _release,
    _reserve,
)

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC).timestamp()


class FakeDatabase:
    def __init__(self):
        self.raw = None
        self.lock = asyncio.Lock()
        self.open_handles = 0
        self.fail_release = False
        self.conn = self
        self.meta = self

    @asynccontextmanager
    async def open(self):
        self.open_handles += 1
        try:
            yield self
        finally:
            self.open_handles -= 1

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            previous = self.raw
            try:
                yield
            except BaseException:
                self.raw = previous
                raise

    async def execute(self, query, params):
        if self.fail_release:
            raise OSError('database unavailable')
        if query.startswith('INSERT') and self.raw is None:
            self.raw = params[1]

    @asynccontextmanager
    async def cursor(self, **kwargs):
        yield self

    async def fetchone(self):
        return {'value': self.raw}

    async def set(self, key, value):
        self.raw = value

    def state(self):
        return json.loads(self.raw)


class CaixinPacingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.database = FakeDatabase()
        self.now = NOW
        self.storage_patch = patch('hippo.sources.caixin_pacing._open_storage', self.database.open)
        self.clock_patch = patch('hippo.sources.caixin_pacing.time.time', lambda: self.now)
        self.random_patch = patch('hippo.sources.caixin_pacing.random.randint', lambda start, end: start)
        for patcher in (self.storage_patch, self.clock_patch, self.random_patch):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_independent_callers_share_a_lease_and_a_completion_interval(self):
        first, second = CaixinPacer(), CaixinPacer()
        async with first.request('list'):
            self.assertEqual(0, self.database.open_handles)
            with self.assertRaises(CaixinPaused):
                async with second.request('article'):
                    self.fail('another caller entered an active request')
            self.now += 10
        self.now += 14
        with self.assertRaises(CaixinPaused) as caught:
            async with second.request('article'):
                self.fail('completion interval was ignored')
        self.assertEqual(1, caught.exception.retry_after_seconds)
        self.now += 1
        async with CaixinPacer().request('article'):
            pass
        self.assertEqual(2, self.database.state()['daily_ops'])
        self.assertEqual(1, self.database.state()['daily_articles'])

    async def test_article_burst_rests_across_new_instances(self):
        for _ in range(3):
            async with CaixinPacer().request('article'):
                self.now += 2
            eligible = self.database.state()['next_at']
            if self.database.state()['daily_articles'] < 3:
                self.now = eligible
        self.assertGreaterEqual(eligible - self.now, 1800)
        self.now += 900
        with self.assertRaises(CaixinPaused):
            async with CaixinPacer().request('list'):
                self.fail('a list request bypassed the article rest')

    async def test_daily_limits_survive_restart_and_do_not_block_free_article_budget_with_lists(self):
        async with CaixinPacer().request('list'):
            pass
        state = self.database.state()
        state.update(daily_articles=DAILY_ARTICLES, next_at=self.now)
        self.database.raw = json.dumps(state)
        with self.assertRaises(CaixinPaused):
            async with CaixinPacer().request('article'):
                self.fail('daily article budget was reset')
        async with CaixinPacer().request('list'):
            pass
        state = self.database.state()
        state.update(daily_ops=DAILY_OPERATIONS, next_at=self.now)
        self.database.raw = json.dumps(state)
        with self.assertRaises(CaixinPaused):
            async with CaixinPacer().request('list'):
                self.fail('daily operation budget was reset')

    async def test_failure_retry_after_is_preserved_and_the_original_error_is_raised(self):
        class RateLimited(SourceError):
            retry_after_seconds = 7200

        original = RateLimited('upstream rejected this request')
        with self.assertRaises(RateLimited) as caught:
            async with CaixinPacer().request('article'):
                self.now += 5
                raise original
        self.assertIs(original, caught.exception)
        self.assertEqual(self.now + 7200, self.database.state()['next_at'])
        self.assertIsNone(self.database.state()['lease_token'])

    async def test_cancellation_consumes_budget_and_enters_a_cooldown(self):
        with self.assertRaises(asyncio.CancelledError):
            async with CaixinPacer().request('article'):
                raise asyncio.CancelledError
        self.assertEqual(1, self.database.state()['daily_articles'])
        self.assertGreaterEqual(self.database.state()['next_at'], self.now + 900)

    async def test_cli_rate_limit_sets_the_shared_cooldown(self):
        process = Mock(
            returncode=1,
            communicate=AsyncMock(
                return_value=(
                    json.dumps({'ok': False, 'error': {'code': 'rate_limited', 'retry_after_secs': 7200}}).encode(),
                    None,
                )
            ),
        )
        with (
            patch('hippo.sources.caixin.asyncio.create_subprocess_exec', AsyncMock(return_value=process)),
            self.assertRaises(CaixinRateLimited),
        ):
            await CaixinSource(pacer=CaixinPacer()).fetch_article(
                'https://international.caixin.com/2026-11-11/102500001.html',
            )
        self.assertEqual(self.now + 7200, self.database.state()['next_at'])

    async def test_probe_reserves_one_article_lease_for_discovery_and_body(self):
        url = 'https://international.caixin.com/2026-11-11/102500001.html'
        source = CaixinSource(pacer=CaixinPacer())
        with patch.object(
            source,
            '_request',
            AsyncMock(
                side_effect=[
                    [{'is_paid': True, 'url': url}],
                    {'article_id': 102500001, 'title': 'Paid', 'content_html': '<p>Full body</p>'},
                ]
            ),
        ) as request:
            self.assertEqual('Paid', await source.probe())
        self.assertEqual(2, request.await_count)
        self.assertEqual(1, self.database.state()['daily_articles'])

    async def test_cleanup_failure_does_not_mask_the_original_exception(self):
        original = RuntimeError('request failed')
        with patch('hippo.sources.caixin_pacing.logger') as logger, self.assertRaises(RuntimeError) as caught:
            async with CaixinPacer().request('article'):
                self.database.fail_release = True
                raise original
        self.assertIs(original, caught.exception)
        logger.warning.assert_called_once()
        self.assertEqual(self.now + 120, self.database.state()['lease_until'])

    async def test_corrupt_state_fails_closed_without_printing_it(self):
        self.database.raw = 'sensitive broken state'
        with self.assertRaises(SourceError) as caught:
            async with CaixinPacer().request('list'):
                self.fail('a broken state reset the limits')
        self.assertNotIn('sensitive', str(caught.exception))

    def test_midnight_preserves_an_active_lease_and_long_rest(self):
        state = {}
        token = _reserve(state, 'article', self.now)
        tomorrow = self.now + 86400
        state.update(next_at=tomorrow + 600, lease_until=tomorrow + 90)
        with self.assertRaises(CaixinPaused):
            _reserve(state, 'list', tomorrow)
        self.assertEqual(token, state['lease_token'])
        self.assertEqual(tomorrow + 600, state['next_at'])
        self.assertEqual(0, state['daily_articles'])
        _release(state, token, tomorrow + 1, None)
        self.assertEqual(tomorrow + 600, state['next_at'])

    def test_a_crashed_request_keeps_its_budget_and_a_stale_release_cannot_clear_the_new_lease(self):
        state = {}
        first = _reserve(state, 'article', self.now)
        second = _reserve(state, 'article', self.now + 121)
        snapshot = dict(state)
        _release(state, first, self.now + 122, RuntimeError('late failure'))
        self.assertEqual(snapshot, state)
        self.assertEqual(second, state['lease_token'])
        self.assertEqual(2, state['daily_articles'])
