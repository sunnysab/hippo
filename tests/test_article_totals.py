"""The article list reports one total, from one source.

The list used to read its ``total`` from the per-account counter and its facet
counts from a live scan, so the two numbers in the same response could disagree
(380,201 against 380,406). Now the total rides along with the facet scan, so an
unfiltered list costs one scan and the two numbers cannot drift apart.
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
    async def test_the_total_comes_from_the_facet_scan(self) -> None:
        rows = [{'id': 1, 'biz': 'gh_1', 'item_show_type': 0, 'read_at': None, 'starred_at': None}]
        facet_rows = [
            {'item_show_type': 0, 'total': 2, 'grand_total': 3},
            {'item_show_type': 5, 'total': 1, 'grand_total': 3},
        ]

        with (
            patch.object(article_queries, 'fetchall_rows', AsyncMock(side_effect=[rows, facet_rows])),
            patch.object(article_queries, 'fetchone_row', AsyncMock(return_value={'total': 3})) as count,
        ):
            result = await _list_articles(SimpleNamespace(), **_list_kwargs())

        self.assertEqual(3, result['total'])
        self.assertEqual(
            result['total'],
            sum(facet['count'] for facet in result['item_show_type_facets']),
        )
        # No second count: the whole point of the window function.
        count.assert_not_awaited()

    async def test_an_unknown_layout_type_still_counts_towards_the_total(self) -> None:
        # The facet list drops types it does not render; the window total must not.
        facet_rows = [
            {'item_show_type': 0, 'total': 2, 'grand_total': 4},
            {'item_show_type': 99, 'total': 2, 'grand_total': 4},
        ]

        with (
            patch.object(article_queries, 'fetchall_rows', AsyncMock(side_effect=[[], facet_rows])),
            patch.object(article_queries, 'fetchone_row', AsyncMock()) as count,
        ):
            result = await _list_articles(SimpleNamespace(), **_list_kwargs())

        self.assertEqual(4, result['total'])
        self.assertEqual([{'item_show_type': 0, 'count': 2}], result['item_show_type_facets'])
        count.assert_not_awaited()

    async def test_selecting_a_layout_type_counts_that_type(self) -> None:
        facet_rows = [{'item_show_type': 0, 'total': 9, 'grand_total': 9}]

        with (
            patch.object(article_queries, 'fetchall_rows', AsyncMock(side_effect=[[], facet_rows])),
            patch.object(article_queries, 'fetchone_row', AsyncMock(return_value={'total': 4})) as count,
        ):
            result = await _list_articles(SimpleNamespace(), **_list_kwargs(item_show_type=5))

        self.assertEqual(4, result['total'])
        count.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
