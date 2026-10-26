"""The worker loop itself, over the real repository, clustering a repeated story.

Every other test in this suite stops at one layer. The two review rejections both
came from exactly that gap, so this one drives ``topic_worker.run_once`` with only
the model and the connection doubled: the backfill, the candidate query, the
lease, the CAS publish and the event wiring are the real code.
"""

import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_topic_repository import Connection

from hippo import topic_worker as worker
from hippo.llm.client import DecisionUnavailableError
from hippo.repositories.topic import TopicRepository

#: The fixture rows must fall inside the worker's 30-day backfill window.
RECENT = 1792916311

VOCABULARY = {'version': 1, 'topics': [{'slug': 'news', 'name': '新闻与时事'}]}

#: Three publishers, one story. Only these two articles are the same story.
ARTICLES = [
    (1, 'biz_a', '中国临时实施氦气出口禁令', '商务部、海关总署决定对氦气实施临时禁止出口管理。'),
    (2, 'biz_b', '突发，氦气临时禁止出口', '海关总署公告，对氦气实施临时出口管制。'),
    (3, 'biz_c', '氦气产业链全梳理', '从提纯到储运的完整梳理，不涉及本次管制。'),
]


class _FakeModel:
    """Stands in for the LLM: article 2 joins whatever article 1 created."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.seen_candidates: list[list[dict]] = []

    async def __call__(self, provider, *, title, digest, vocabulary, candidates=None):
        self.seen_candidates.append(list(candidates or []))
        topics = [{'slug': 'news', 'name': '新闻与时事'}]
        if title.startswith('中国临时实施'):
            return topics, {'action': 'new', 'title': '中国临时实施氦气出口禁令'}
        if title.startswith('突发'):
            # Join through the candidate list only -- exactly what the worker offers.
            target = next((item['id'] for item in candidates or []), None)
            if target is None:
                return topics, None
            return topics, {'action': 'join', 'id': target}
        return topics, None


class WorkerPipelineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = Connection()
        self.repo = TopicRepository(self.connection)
        self.addCleanup(self.connection.db.close)
        for pk, biz, title, digest in ARTICLES:
            self.connection.db.execute('INSERT OR IGNORE INTO accounts VALUES (?, ?)', (biz, biz))
            # Article 1 already exists in the fixture; rewrite its title and digest
            # rather than inserting a second row for the same id.
            self.connection.db.execute(
                'INSERT INTO articles (id, title, digest, publish_at, biz) VALUES (?, ?, ?, ?, ?)'
                ' ON CONFLICT (id) DO UPDATE SET title = excluded.title, digest = excluded.digest,'
                ' biz = excluded.biz, publish_at = excluded.publish_at',
                (pk, title, digest, RECENT, biz),
            )
            # Reader 1 follows all three publishers, so the event is visible to them.
            self.connection.db.execute(
                'INSERT OR IGNORE INTO subscription (user_id, biz) VALUES (1, ?)', (biz,)
            )
        self.model = _FakeModel()

    @asynccontextmanager
    async def _storage(self):
        @asynccontextmanager
        async def transaction():
            yield

        yield SimpleNamespace(topics=self.repo, transaction=transaction)

    async def _drain(self) -> int:
        """Run the real loop until the queue is empty, like the web process does."""
        runs = 0
        while await worker.run_once():
            runs += 1
            if runs > 10:  # guard against a loop that never terminates
                break
        return runs

    async def test_a_repeated_story_becomes_one_visible_event(self):
        with (
            patch.object(worker, 'open_storage', self._storage),
            patch.object(worker, '_vocabulary', AsyncMock(return_value=VOCABULARY)),
            patch.object(
                worker,
                'resolve_decision_provider',
                AsyncMock(side_effect=DecisionUnavailableError('none')),
            ),
            patch.object(worker, 'resolve_provider', AsyncMock(return_value={'model': 'fake', 'kind': 'chat'})),
            patch.object(worker, 'classify', self.model),
        ):
            runs = await self._drain()

        self.assertEqual(3, runs)

        # Exactly one event, and it is visible because two distinct publishers joined it.
        visible = [event for event in [await self.repo.public_event(i, 1) for i in range(1, 6)] if event]
        self.assertEqual(1, len(visible), f'expected one visible event, got {visible}')
        self.assertEqual('中国临时实施氦气出口禁令', visible[0]['title'])
        self.assertEqual(2, visible[0]['source_count'])
        self.assertEqual(2, visible[0]['article_count'])

    async def test_the_second_article_was_offered_the_event_the_first_one_created(self):
        with (
            patch.object(worker, 'open_storage', self._storage),
            patch.object(worker, '_vocabulary', AsyncMock(return_value=VOCABULARY)),
            patch.object(
                worker,
                'resolve_decision_provider',
                AsyncMock(side_effect=DecisionUnavailableError('none')),
            ),
            patch.object(worker, 'resolve_provider', AsyncMock(return_value={'model': 'fake', 'kind': 'chat'})),
            patch.object(worker, 'classify', self.model),
        ):
            await self._drain()

        # Article 1 saw no candidates; article 2 must have seen article 1's event.
        self.assertEqual([], self.model.seen_candidates[0])
        self.assertEqual(1, len(self.model.seen_candidates[1]))

    async def test_the_unrelated_article_stays_out_of_the_story(self):
        with (
            patch.object(worker, 'open_storage', self._storage),
            patch.object(worker, '_vocabulary', AsyncMock(return_value=VOCABULARY)),
            patch.object(
                worker,
                'resolve_decision_provider',
                AsyncMock(side_effect=DecisionUnavailableError('none')),
            ),
            patch.object(worker, 'resolve_provider', AsyncMock(return_value={'model': 'fake', 'kind': 'chat'})),
            patch.object(worker, 'classify', self.model),
        ):
            await self._drain()

        linked = self.connection.db.execute(
            'SELECT article_pk FROM article_event_link ORDER BY article_pk'
        ).fetchall()
        self.assertEqual([1, 2], [row[0] for row in linked])


if __name__ == '__main__':
    unittest.main()
