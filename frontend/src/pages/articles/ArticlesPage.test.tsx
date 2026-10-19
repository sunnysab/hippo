import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ArticlesProvider } from '../../store/articles';
import { ToastProvider } from '../../hooks/useToast';
import { I18nProvider } from '../../i18n';
import zhCN from '../../i18n/zh-CN.json';
import { ArticlesPage } from './ArticlesPage';

const apiGetMock = vi.fn();
const apiSendMock = vi.fn();

vi.mock('../../api', async () => {
  const actual = await vi.importActual<typeof import('../../api')>('../../api');
  return {
    ...actual,
    apiGet: (...args: unknown[]) => apiGetMock(...args),
    apiSend: (...args: unknown[]) => apiSendMock(...args),
  };
});

/** jsdom has neither of these; the page only needs them to exist. */
class StubIntersectionObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const stubMatchMedia = (query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addEventListener: () => {},
  removeEventListener: () => {},
  addListener: () => {},
  removeListener: () => {},
  dispatchEvent: () => false,
});

/** The reading layout and the resizer persist to localStorage, which this
 *  environment does not provide. */
const stubStorage = () => {
  const entries = new Map<string, string>();
  return {
    getItem: (key: string) => entries.get(key) ?? null,
    setItem: (key: string, value: string) => { entries.set(key, String(value)); },
    removeItem: (key: string) => { entries.delete(key); },
    clear: () => { entries.clear(); },
    key: (index: number) => Array.from(entries.keys())[index] ?? null,
    get length() { return entries.size; },
  };
};

const renderPage = (path = '/articles') => render(
  <MemoryRouter initialEntries={[path]}>
    <I18nProvider>
      <ToastProvider>
        <ArticlesProvider>
          <ArticlesPage />
        </ArticlesProvider>
      </ToastProvider>
    </I18nProvider>
  </MemoryRouter>,
);

/** The run button lives in the list header, beside the filter toggle. */
const updateButton = () => screen.getByRole('button', { name: zhCN['articles.updateNow'] });

const clickUpdate = async () => {
  await act(async () => {
    fireEvent.click(updateButton());
  });
};

describe('ArticlesPage update entry', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('IntersectionObserver', StubIntersectionObserver);
    vi.stubGlobal('matchMedia', stubMatchMedia);
    vi.stubGlobal('localStorage', stubStorage());
    apiGetMock.mockImplementation(async (path: string) => {
      if (path.startsWith('/api/group')) return { groups: [] };
      if (path.startsWith('/api/article')) {
        return { articles: [], total: 0, item_show_type_facets: [] };
      }
      return {};
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('updates everything the reader follows when no filter is set', async () => {
    apiSendMock.mockResolvedValue({ task_id: 'task-1', status: 'queued' });

    renderPage();
    await clickUpdate();

    expect(apiSendMock).toHaveBeenCalledWith('/api/settings/run', 'POST', {}, expect.anything());
  });

  it('updates only the source the list is filtered to', async () => {
    apiSendMock.mockResolvedValue({ task_id: 'task-1', status: 'queued' });

    renderPage('/articles?source=bilibili');
    await clickUpdate();

    expect(apiSendMock).toHaveBeenCalledWith(
      '/api/settings/run',
      'POST',
      { source: 'bilibili' },
      expect.anything(),
    );
  });

  it('updates only the group the list is filtered to', async () => {
    apiSendMock.mockResolvedValue({ task_id: 'task-1', status: 'queued' });

    renderPage('/articles?group=7');
    await clickUpdate();

    expect(apiSendMock).toHaveBeenCalledWith(
      '/api/settings/run',
      'POST',
      { group_id: 7 },
      expect.anything(),
    );
  });

  it('keeps source and group together when both are filtered', async () => {
    apiSendMock.mockResolvedValue({ task_id: 'task-1', status: 'queued' });

    renderPage('/articles?source=bilibili&group=7');
    await clickUpdate();

    expect(apiSendMock).toHaveBeenCalledWith(
      '/api/settings/run',
      'POST',
      { source: 'bilibili', group_id: 7 },
      expect.anything(),
    );
  });

  it('asks for group counts inside the source the list is filtered to', async () => {
    renderPage('/articles?source=bilibili');

    // 左栏分组后面的数字是“这个来源下这个组有几个号”，请求里必须带上来源。
    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledWith('/api/group?source=bilibili');
    });
  });

  it('limits the update to one account when an account is filtered', async () => {
    apiSendMock.mockResolvedValue({ task_id: 'task-1', status: 'queued' });

    renderPage('/articles?account=biz-a');
    await clickUpdate();

    expect(apiSendMock).toHaveBeenCalledWith(
      '/api/settings/run',
      'POST',
      { biz_list: ['biz-a'] },
      expect.anything(),
    );
  });

  it('shows the job it is following and keeps the run button busy', async () => {
    apiSendMock.mockResolvedValue({ task_id: 'task-1', status: 'queued' });

    renderPage();
    expect((updateButton() as HTMLButtonElement).disabled).toBe(false);

    await clickUpdate();

    await waitFor(() => {
      expect(document.querySelector('#article-sync-status')?.textContent)
        .toBe(zhCN['sync.status.pending']);
    });
    expect((updateButton() as HTMLButtonElement).disabled).toBe(true);
  });
});
