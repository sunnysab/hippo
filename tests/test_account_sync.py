"""Discovery completion must never hide a body backlog or invent a login state."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hippo.account_sync import account_sync_state, enrich_account_sync


@pytest.mark.parametrize(
    ('state', 'job', 'enabled', 'expected'),
    [
        ('done', 'success', True, 'complete'),
        ('pending', 'running', True, 'running'),
        ('pending', 'queued', True, 'queued'),
        ('pending', 'failed', True, 'failed'),
        ('pending', None, False, 'paused'),
        ('paused', 'cancelled', True, 'paused'),
        ('paused', 'cancelling', True, 'cancelling'),
        ('pending', None, True, 'pending'),
    ],
)
def test_history_state_is_independent_of_body_backlog(state, job, enabled, expected):
    result = account_sync_state(
        {
            'source': 'weixin',
            'backfill_state': state,
            'history_job_status': job,
            'history_enabled': enabled,
            'pending': 1112,
            'processing': 2,
            'failed': 3,
            'last_synced_at': None,
        }
    )
    assert result['history']['status'] == expected
    assert result['articles'] == {'pending': 1112, 'processing': 2, 'failed': 3}
    assert result['last_checked_at'] is None


def test_other_sources_do_not_claim_a_wechat_backfill():
    assert account_sync_state({'source': 'youtube', 'backfill_state': 'pending'})['history']['status'] == 'unsupported'


def test_visible_accounts_are_enriched_in_one_bounded_query():
    asyncio.run(_visible_accounts_are_enriched_in_one_bounded_query())


async def _visible_accounts_are_enriched_in_one_bounded_query():
    cursor = AsyncMock()
    cursor.__aenter__.return_value = cursor
    cursor.fetchall.return_value = [
        {'biz': 'one', 'source': 'weixin', 'backfill_state': 'done', 'pending': 1112},
        {'biz': 'two', 'source': 'weixin', 'backfill_state': 'pending', 'history_job_status': 'running'},
    ]
    rows = [{'biz': 'one'}, {'biz': 'two'}]
    await enrich_account_sync(SimpleNamespace(cursor=lambda **_: cursor), rows)
    cursor.execute.assert_awaited_once()
    assert cursor.execute.await_args.args[1] == (['one', 'two'], ['one', 'two'], ['one', 'two'])
    assert rows[0]['sync']['history']['status'] == 'complete'
    assert rows[0]['sync']['articles']['pending'] == 1112
    assert rows[1]['sync']['history']['status'] == 'running'


def test_empty_page_never_queries_the_queue():
    asyncio.run(_empty_page_never_queries_the_queue())


async def _empty_page_never_queries_the_queue():
    conn = SimpleNamespace(cursor=AsyncMock())
    await enrich_account_sync(conn, [])
    conn.cursor.assert_not_called()
