import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AccountCard } from './AccountCard';
import type { Account } from '../../store/shared';

const groupsStateMock = vi.fn();

vi.mock('../../store/groups', () => ({
  useGroupsState: () => groupsStateMock(),
}));

vi.mock('../../i18n', () => ({
  useI18n: () => ({ t: (key: string, fallback?: string) => fallback || key }),
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
  sync_interval_days: null,
  article_count: 0,
  backfill_pending: false,
  backfill_running: false,
};

describe('AccountCard history backfill state', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    groupsStateMock.mockReturnValue({ state: { selectedAccounts: [] }, dispatch: vi.fn() });
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
