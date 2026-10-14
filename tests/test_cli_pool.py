"""Long-lived resources a short command must still release on the way out.

A CLI command runs its own ``asyncio.run``. Anything it opens and leaves open is
torn down by the loop's cancellation pass instead, which hangs when a connection
attempt is still in flight -- the command then never exits.
"""

import unittest
from unittest.mock import AsyncMock, patch

from hippo import cli


class CoroClosesPoolTest(unittest.TestCase):
    def test_the_pool_is_closed_on_the_way_out(self) -> None:
        events: list[str] = []

        async def work() -> str:
            events.append('ran')
            return 'done'

        with patch.object(cli, 'close_pool', new=AsyncMock(side_effect=lambda: events.append('closed'))):
            result = cli.coro(work)()

        self.assertEqual('done', result)
        self.assertEqual(['ran', 'closed'], events)

    def test_the_pool_is_closed_even_when_the_command_raises(self) -> None:
        closed = []

        async def boom() -> None:
            raise RuntimeError('command failed')

        with (
            patch.object(cli, 'close_pool', new=AsyncMock(side_effect=lambda: closed.append(1))),
            self.assertRaises(RuntimeError),
        ):
            cli.coro(boom)()

        self.assertEqual([1], closed, 'a failure must still release the pool')
