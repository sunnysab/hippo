import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AccountCard } from './AccountCard';
import zhCN from '../../i18n/zh-CN.json';
import type { Account } from '../../store/shared';

const groupsStateMock = vi.fn();

vi.mock('../../store/groups', () => ({
  useGroupsState: () => groupsStateMock(),
}));

vi.mock('../../i18n', () => ({
  useI18n: () => ({ t: (key: string, fallback?: string) => zhCN[key as keyof typeof zhCN] || fallback || key }),
}));

vi.mock('../../hooks/useToast', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
}));

const baseAccount: Account = {
  biz: 'Mz1',
  source: 'weixin',
  nickname: '中投数研',
  alias: null,
  round_head_img: '',
  avatar_url: '/api/account/Mz1/avatar',
  group_id: 1,
  is_disabled: false,
  last_synced_at: null,
  latest_publish_at: null,
  sync_interval_days: null,
  article_count: 0,
  backfill_pending: false,
  backfill_running: false,
};

describe('AccountCard history backfill state', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
    groupsStateMock.mockReturnValue({ state: { selectedAccounts: [] }, dispatch: vi.fn() });
  });

  it('hides the update interval for WeChat subscriptions', () => {
    render(<AccountCard account={baseAccount} />);
    expect(document.querySelector('.account-sync-interval')).toBeNull();
  });

  it('keeps source defaults and daily intervals available for polling sources', () => {
    render(<AccountCard account={{ ...baseAccount, source: 'feed' }} />);
    const interval = document.querySelector('.account-sync-interval') as HTMLSelectElement;
    expect(interval.value).toBe('');
    expect(interval.options[0].textContent).toBe('跟随来源设置');
    expect(interval.options[1].textContent).toBe('每天一次');
  });

  it('shows the latest publication separately from the last sync', () => {
    vi.spyOn(Date, 'now').mockReturnValue(new Date('2026-10-20T08:00:00+08:00').getTime());
    render(<AccountCard account={{
      ...baseAccount,
      latest_publish_at: '2026-10-20T01:00:00+08:00',
      last_synced_at: '2026-10-20T05:49:00+08:00',
    }} />);

    expect(screen.getByText(zhCN['accounts.lastUpdated'].replace('{time}', zhCN['time.hoursAgo'].replace('{n}', '7')))).toBeTruthy();
    expect(screen.getByText(zhCN['accounts.lastSynced'].replace('{time}', zhCN['time.hoursAgo'].replace('{n}', '2')))).toBeTruthy();
  });

  it('does not substitute a sync time when there is no publication time', () => {
    vi.spyOn(Date, 'now').mockReturnValue(new Date('2026-10-20T08:00:00+08:00').getTime());
    render(<AccountCard account={{ ...baseAccount, last_synced_at: '2026-10-20T05:49:00+08:00' }} />);

    expect(screen.getByText(zhCN['accounts.lastUpdatedEmpty'])).toBeTruthy();
    expect(screen.queryByText(zhCN['accounts.lastUpdated'].replace('{time}', zhCN['time.hoursAgo'].replace('{n}', '2')))).toBeNull();
    expect(screen.getByText(zhCN['accounts.lastSynced'].replace('{time}', zhCN['time.hoursAgo'].replace('{n}', '2')))).toBeTruthy();
  });

  it('marks the card while the history is being fetched', () => {
    render(<AccountCard account={{ ...baseAccount, backfill_pending: true, backfill_running: true }} />);

    expect(screen.getByText(zhCN['accounts.syncing'])).toBeTruthy();
    expect(screen.getByText(zhCN['accounts.syncingShort'])).toBeTruthy();
    expect(screen.queryByText(zhCN['accounts.lastSyncedEmpty'])).toBeNull();
    expect(screen.queryByText(zhCN['accounts.syncQueued'])).toBeNull();
  });

  it('marks it as queued before the fetch starts', () => {
    render(<AccountCard account={{ ...baseAccount, backfill_pending: true }} />);

    expect(screen.getByText(zhCN['accounts.syncQueued'])).toBeTruthy();
    expect(screen.getByText(zhCN['accounts.syncQueuedShort'])).toBeTruthy();
    expect(screen.queryByText(zhCN['accounts.syncingShort'])).toBeNull();
    expect(screen.queryByText(zhCN['accounts.lastSyncedEmpty'])).toBeNull();
  });

  it('keeps identifiers and counts in a collapsible details panel', () => {
    render(<AccountCard account={{ ...baseAccount, article_count: 1187 }} />);

    const summary = screen.getByText(zhCN['accounts.syncDetails']);
    const details = summary.closest('details')!;
    expect(details.open).toBe(false);
    expect(details.contains(screen.getByText('Mz1'))).toBe(true);
    expect(details.contains(screen.getByText(zhCN['accounts.articleCount'].replace('{n}', '1187')))).toBe(true);
    fireEvent.click(summary);
    expect(details.open).toBe(true);
  });

  it.each([
    { backfill_running: true, backfill_pending: true },
    { backfill_running: false, backfill_pending: true },
  ])('keeps the previous sync time during backfill: %j', (backfill) => {
    vi.spyOn(Date, 'now').mockReturnValue(new Date('2026-10-20T08:00:00+08:00').getTime());
    render(<AccountCard account={{ ...baseAccount, ...backfill, last_synced_at: '2026-10-20T05:49:00+08:00' }} />);

    expect(screen.getByText(zhCN['accounts.lastSynced'].replace('{time}', zhCN['time.hoursAgo'].replace('{n}', '2')))).toBeTruthy();
  });

  it('stays quiet once there is nothing left to backfill', () => {
    render(<AccountCard account={baseAccount} />);

    expect(screen.getByText(zhCN['accounts.lastSyncedEmpty'])).toBeTruthy();
    expect(screen.queryByText(zhCN['accounts.syncing'])).toBeNull();
    expect(screen.queryByText(zhCN['accounts.syncQueued'])).toBeNull();
  });
});
