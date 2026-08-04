from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from scripts.probe_weread_rate_limit import probe_until_available


class ProbeWereadRateLimitTest(unittest.IsolatedAsyncioTestCase):
    async def test_retries_frequency_limit_until_recovered(self) -> None:
        api = AsyncMock()
        api.list_articles.side_effect = [
            RuntimeError('WeRead API error -2014: frequency limit'),
            {'data': [{}]},
        ]
        sleep = AsyncMock()

        result = await probe_until_available(
            api,
            object(),
            biz='test-biz',
            interval_seconds=900,
            max_attempts=2,
            sleep=sleep,
        )

        self.assertEqual(result, 0)
        self.assertEqual(api.list_articles.await_count, 2)
        sleep.assert_awaited_once_with(900)


if __name__ == '__main__':
    unittest.main()
