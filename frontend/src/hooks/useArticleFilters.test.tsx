import { act, renderHook, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import { ArticlesProvider, useArticlesState } from '../store/articles';
import { I18nProvider } from '../i18n';
import { useArticleFilters } from './useArticleFilters';

const apiGetMock = vi.fn();
vi.mock('../api', () => ({
  apiGet: (...args: unknown[]) => apiGetMock(...args),
  apiSend: vi.fn().mockResolvedValue({}),
  isAuthError: () => false,
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function renderFilters(path = '/articles') {
  return renderHook(() => ({ ...useArticleFilters(), ...useArticlesState() }), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <MemoryRouter initialEntries={[path]}>
        <I18nProvider><ArticlesProvider>{children}</ArticlesProvider></I18nProvider>
      </MemoryRouter>
    ),
  });
}

const list = (source: string, id = 1) => ({ articles: [{ id, source }], total: 1 });

describe('useArticleFilters', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiGetMock.mockImplementation(async (path: string) => path.startsWith('/api/group')
      ? { groups: [] } : { articles: [], total: 0 });
    vi.stubGlobal('matchMedia', (media: string) => ({
      matches: false, media, addEventListener() {}, removeEventListener() {},
    }));
  });

  afterEach(() => { vi.unstubAllGlobals(); });

  it('loads the newest source while an older list request is pending', async () => {
    const old = deferred<unknown>();
    apiGetMock.mockImplementation((path: string) => {
      if (!path.startsWith('/api/article?')) return Promise.resolve({ groups: [] });
      return path.includes('source=youtube') ? Promise.resolve(list('youtube', 2)) : old.promise;
    });
    const { result } = renderFilters();
    await waitFor(() => expect(result.current.state.isArticleLoading).toBe(true));
    await act(async () => result.current.updateFilters({ source: 'youtube' }));
    await waitFor(() => expect(result.current.state.articles[0]?.source).toBe('youtube'));
    await act(async () => old.resolve(list('feed')));
    expect(result.current.state.articles[0]?.source).toBe('youtube');
  });

  it('does not clear the loading state when a superseded request finishes', async () => {
    const old = deferred<unknown>();
    const latest = deferred<unknown>();
    apiGetMock.mockImplementation((path: string) => {
      if (!path.startsWith('/api/article?')) return Promise.resolve({ groups: [] });
      return path.includes('source=youtube') ? latest.promise : old.promise;
    });
    const { result } = renderFilters();
    await waitFor(() => expect(result.current.state.isArticleLoading).toBe(true));
    await act(async () => result.current.updateFilters({ source: 'youtube' }));
    await act(async () => old.resolve(list('feed')));
    expect(result.current.state.isArticleLoading).toBe(true);
    await act(async () => latest.resolve(list('youtube')));
    expect(result.current.state.isArticleLoading).toBe(false);
  });

  it('does not start duplicate pagination while a list request is pending', async () => {
    const pending = deferred<unknown>();
    apiGetMock.mockImplementation((path: string) => path.startsWith('/api/article?')
      ? pending.promise : Promise.resolve({ groups: [] }));
    const { result } = renderFilters();
    await waitFor(() => expect(result.current.state.isArticleLoading).toBe(true));
    await act(async () => result.current.loadArticles(result.current.filters, false));
    expect(apiGetMock.mock.calls.filter(([path]) => path.startsWith('/api/article?'))).toHaveLength(1);
    await act(async () => pending.resolve(list('feed')));
  });

  it('clears the previous account when the source changes without a group', async () => {
    const { result } = renderFilters('/articles?source=feed&account=feed-account');
    await waitFor(() => expect(apiGetMock).toHaveBeenCalled());
    await act(async () => result.current.updateFilters({ source: 'youtube' }));
    expect(result.current.filters.accountBiz).toBe('');
    await waitFor(() => expect(apiGetMock.mock.calls.some(([path]) => (
      path.startsWith('/api/article?') && path.includes('source=youtube') && !path.includes('biz=')
    ))).toBe(true));
  });

  it('keeps an explicit account selection when the source did not change', async () => {
    const { result } = renderFilters('/articles?source=feed&account=feed-account');
    await waitFor(() => expect(apiGetMock).toHaveBeenCalled());
    await act(async () => result.current.updateFilters({ source: 'feed' }));
    expect(result.current.filters.accountBiz).toBe('feed-account');
  });

  it('ignores an old group account response after switching groups', async () => {
    const old = deferred<unknown>();
    apiGetMock.mockImplementation((path: string) => {
      if (path.startsWith('/api/group')) return Promise.resolve({ groups: [
        { id: 1, name: 'One', account_count: 1 }, { id: 2, name: 'Two', account_count: 1 },
      ] });
      if (path.includes('/api/account?group_id=1')) return old.promise;
      if (path.includes('/api/account?group_id=2')) return Promise.resolve({ accounts: [{ biz: 'two', nickname: 'Two' }] });
      return Promise.resolve({ articles: [], total: 0 });
    });
    const { result } = renderFilters('/articles?group=1');
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/account?group_id=1&page_size=200'));
    await act(async () => result.current.updateFilters({ groupId: '2' }));
    await waitFor(() => expect(result.current.accountOptions[0]?.value).toBe('two'));
    await act(async () => old.resolve({ accounts: [{ biz: 'one', nickname: 'One' }] }));
    expect(result.current.accountOptions[0]?.value).toBe('two');
  });

  it('reloads group accounts inside the selected source', async () => {
    apiGetMock.mockImplementation(async (path: string) => {
      if (path.startsWith('/api/group')) return { groups: [{ id: 1, name: 'One', account_count: 1 }] };
      if (path.startsWith('/api/account?')) return { accounts: [] };
      return { articles: [], total: 0 };
    });
    const { result } = renderFilters('/articles?group=1&source=feed');
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/account?group_id=1&page_size=200&source=feed'));
    await act(async () => result.current.updateFilters({ source: 'youtube' }));
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/account?group_id=1&page_size=200&source=youtube'));
  });
});
