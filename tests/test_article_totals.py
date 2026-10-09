"""The article list reports one total, from one source.

The list used to read its ``total`` from the per-account counter and its facet
counts from a live scan, so the two numbers in the same response could disagree
(380,201 against 380,406). They are now counted by the same query.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo import article_queries
from hippo.article_queries import _list_articles


def _list_kwargs(**overrides):
    kwargs = {
        'user_id': 1,
        'group_ids': None,
        'biz': None,
        'item_show_type': None,
        'query': None,
        'exclude_keywords': None,
        'since_ts': None,
        'until_ts': None,
        'sort_mode': 'publish_at_desc',
        'page': 1,
        'page_size': 20,
    }
    kwargs.update(overrides)
    return kwargs


class TotalMatchesFacetsTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_total_is_the_sum_of_the_facets(self) -> None:
        rows = [{'id': 1, 'biz': 'gh_1', 'item_show_type': 0, 'read_at': None}]
        facet_rows = [
            {'item_show_type': 0, 'total': 2},
            {'item_show_type': 5, 'total': 1},
        ]

        with (
            patch.object(article_queries, 'fetchall_rows', AsyncMock(side_effect=[rows, facet_rows])),
            patch.object(article_queries, 'fetchone_row', AsyncMock(return_value={'total': 3})),
        ):
            result = await _list_articles(SimpleNamespace(), **_list_kwargs())

        self.assertEqual(3, result['total'])
        self.assertEqual(
            result['total'],
            sum(facet['count'] for facet in result['item_show_type_facets']),
        )

    async def test_the_counter_is_not_consulted_even_without_filters(self) -> None:
        # No active filter used to mean "use the cached counter"; the live count
        # is what keeps the response self-consistent.
        with (
            patch.object(article_queries, 'fetchall_rows', AsyncMock(side_effect=[[], []])),
            patch.object(article_queries, 'fetchone_row', AsyncMock(return_value={'total': 7})) as count,
        ):
            result = await _list_articles(SimpleNamespace(), **_list_kwargs())

        self.assertEqual(7, result['total'])
        self.assertNotIn('article_count', count.await_args.args[1])


if __name__ == '__main__':
    unittest.main()
