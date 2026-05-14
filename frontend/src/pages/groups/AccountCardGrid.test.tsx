import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
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
