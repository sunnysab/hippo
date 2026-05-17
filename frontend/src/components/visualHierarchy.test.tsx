import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { GroupList } from '../pages/groups/GroupList';
import { BatchActions } from '../pages/groups/BatchActions';
import { ActiveTaskPanel } from '../pages/settings/ActiveTaskPanel';
import { ArticleFilterSummary } from '../pages/articles/ArticleFilterSummary';
import { GroupHeader } from '../pages/groups/GroupHeader';
import { ArticleTypeFacets } from '../pages/articles/ArticleTypeFacets';

const groupsStateMock = vi.fn();
const settingsStateMock = vi.fn();
const articlesStateMock = vi.fn();
const tMock = vi.fn((key: string, fallback?: string) => fallback || key);
const mediaQueryMock = vi.fn(() => false);

vi.mock('../store/groups', () => ({
  useGroupsState: () => groupsStateMock(),
}));

vi.mock('../store/settings', () => ({
  useSettingsState: () => settingsStateMock(),
}));

vi.mock('../store/articles', () => ({
  useArticlesState: () => articlesStateMock(),
}));

vi.mock('../i18n', () => ({
  useI18n: () => ({ t: tMock }),
}));

vi.mock('../hooks/useToast', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

vi.mock('../hooks/useMediaQuery', () => ({
  useMediaQuery: () => mediaQueryMock(),
}));

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn(),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
}));

describe('visual hierarchy semantics', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('uses low-emphasis count styling for group and batch counters', () => {
    groupsStateMock
      .mockReturnValueOnce({
        state: {
          groups: [{ id: 1, name: 'Ops', account_count: 3 }],
          selectedGroupId: null,
        },
        dispatch: vi.fn(),
      })
      .mockReturnValueOnce({
        state: {
          groups: [],
          selectedGroupId: null,
          accounts: [],
          selectedAccounts: ['a', 'b'],
        },
        dispatch: vi.fn(),
      });

    const { container } = render(
      <>
        <GroupList onSync={vi.fn()} onSelect={vi.fn()} />
        <BatchActions />
      </>,
    );

    expect(screen.getByText('3').className).toContain('meta-count');
    expect(container.querySelector('#batch-count')?.className).toContain('meta-count');
    expect(container.querySelector('.badge')).toBeNull();
  });

  it('keeps only status as badge and renders task counts as low-emphasis meta', () => {
    settingsStateMock.mockReturnValue({
      state: {
        syncTasks: [{
          task_id: 't1',
          status: 'running',
          created_at: '2026-05-02T12:00:00.000Z',
          started_at: '2026-05-02T12:00:00.000Z',
          finished_at: null,
          error: null,
          trigger_type: 'manual',
          last_log: null,
          accounts_total: 2,
          accounts_done: 1,
          current_account: { biz: 'biz-1', nickname: 'Account A' },
          accounts: [{
            biz: 'biz-1',
            nickname: 'Account A',
            status: 'running',
            saved: 0,
            page_count: 0,
            article_current: 1,
            article_total: 2,
            updated_at: '2026-05-02T12:00:00.000Z',
            skip_reason: '',
          }],
        }],
      },
    });

    const { container } = render(<ActiveTaskPanel />);

    expect(container.querySelector('.sync-status-badge')).toBeTruthy();
    expect(screen.getAllByText('1/2').every((node) => node.className.includes('meta-count'))).toBe(true);
  });

  it('renders filter summary details as meta notes instead of pills', () => {
    render(
      <ArticleFilterSummary
        filters={{
          groupId: '1',
          accountBiz: '',
          itemShowType: '',
          search: '',
          sort: '',
        }}
        groupOptions={[{ value: '1', label: 'Team Alpha' }]}
        accountOptions={[]}
        total={12}
      />,
    );

    expect(screen.getByText('Group: Team Alpha').className).toContain('meta-note');
  });

  it('renders the group id as low-emphasis metadata instead of a chip surface', () => {
    groupsStateMock.mockReturnValue({
      state: {
        groups: [{
          id: 7,
          name: 'Team Seven',
          account_count: 5,
          article_count: 9,
        }],
        selectedGroupId: 7,
      },
    });

    render(
      <GroupHeader
        accountQuery=""
        onAccountQueryChange={vi.fn()}
        onOpenAccountSearch={vi.fn()}
        onOpenRename={vi.fn()}
        onOpenDelete={vi.fn()}
      />,
    );

    expect(screen.getByText('ID: 7').className).toContain('meta-note-button');
  });

  it('marks article type facets as toggle-style controls instead of filled chips', () => {
    articlesStateMock.mockReturnValue({
      state: {
        typeFacetsExpanded: true,
        lastFacetPayload: {
          total: 12,
          item_show_type_facets: [{ item_show_type: 5, count: 4 }],
        },
      },
      dispatch: vi.fn(),
    });

    render(
      <ArticleTypeFacets
        activeType="5"
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByRole('button', { name: /Video Share/i }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('button', { name: /All Types/i }).getAttribute('aria-pressed')).toBe('false');
  });
});
