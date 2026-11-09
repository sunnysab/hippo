import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AccountCardGrid } from './AccountCardGrid';

const groupsStateMock = vi.fn();
const loadAccountsMock = vi.fn();
const tMock = vi.fn((key: string, fallback?: string) => fallback || key);

vi.mock('../../store/groups', () => ({
  useGroupsState: () => groupsStateMock(),
  useGroupsActions: () => ({ loadAccounts: loadAccountsMock }),
}));

vi.mock('../../i18n', () => ({
  useI18n: () => ({ t: tMock }),
}));

vi.mock('./AccountCard', () => ({
  AccountCard: ({ account }: { account: { nickname: string } }) => <div>{account.nickname}</div>,
}));

describe('AccountCardGrid', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => { vi.useRealTimers(); });

  it('polls active backfills even when filtered out and stops after completion', async () => {
    vi.useFakeTimers();
    const account = { biz: 'one', nickname: 'Alpha', alias: null, backfill_pending: true };
    const state = { selectedGroupId: null, accounts: [account] };
    groupsStateMock.mockReturnValue({ state });
    const { rerender } = render(<AccountCardGrid query="no match" />);
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(loadAccountsMock).toHaveBeenCalledWith(true, expect.any(AbortSignal));
    state.accounts = [{ ...account, backfill_pending: false }];
    rerender(<AccountCardGrid query="no match" />);
    await act(() => vi.advanceTimersByTimeAsync(15000));
    expect(loadAccountsMock).toHaveBeenCalledTimes(1);
  });

  it('retries failed polling and stops when unmounted', async () => {
    vi.useFakeTimers();
    loadAccountsMock.mockRejectedValueOnce(new Error('offline'));
    groupsStateMock.mockReturnValue({ state: { selectedGroupId: null, accounts: [
      { biz: 'one', nickname: 'Alpha', alias: null, backfill_running: true },
    ] } });
    const { unmount } = render(<AccountCardGrid query="" />);
    await act(() => vi.advanceTimersByTimeAsync(10000));
    expect(loadAccountsMock).toHaveBeenCalledTimes(2);
    unmount();
    await act(() => vi.advanceTimersByTimeAsync(10000));
    expect(loadAccountsMock).toHaveBeenCalledTimes(2);
  });

  it('filters accounts without crashing when alias is null', () => {
    groupsStateMock.mockReturnValue({
      state: {
        selectedGroupId: null,
        accounts: [
          {
            biz: 'gh_1',
            nickname: 'Alpha',
            alias: null,
            avatar_url: '/avatar/gh_1',
          },
          {
            biz: 'gh_2',
            nickname: 'Beta',
            alias: 'letters',
            avatar_url: '/avatar/gh_2',
          },
        ],
      },
    });

    render(<AccountCardGrid query="let" />);

    expect(screen.getByText('Beta')).toBeTruthy();
  });
});
