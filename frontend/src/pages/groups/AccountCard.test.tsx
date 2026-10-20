import { render, screen } from '@testing-library/react';
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

    expect(screen.getByText('同步中')).toBeTruthy();
  });

  it('marks it as queued before the fetch starts', () => {
    render(<AccountCard account={{ ...baseAccount, backfill_pending: true }} />);

    expect(screen.getByText('排队中')).toBeTruthy();
  });

  it('stays quiet once there is nothing left to backfill', () => {
    render(<AccountCard account={baseAccount} />);

    expect(screen.queryByText('同步中')).toBeNull();
    expect(screen.queryByText('排队中')).toBeNull();
  });
});
