import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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

let viewportWidth = 1440;
const mediaListeners = new Set<() => void>();
const stubMatchMedia = (query: string) => ({
  matches: query.includes('min-width') ? viewportWidth >= Number(query.match(/(\d+)px/)?.[1])
    : query.includes('max-width') ? viewportWidth <= Number(query.match(/(\d+)px/)?.[1]) : false,
  media: query,
  addEventListener: (_event: string, listener: () => void) => { mediaListeners.add(listener); },
  removeEventListener: (_event: string, listener: () => void) => { mediaListeners.delete(listener); },
});

const resizeViewport = (width: number) => {
  viewportWidth = width;
  act(() => { for (const listener of [...mediaListeners]) listener(); });
};

const articlePayload = {
  article: { id: 8101708, biz: 'youtube-channel', title: 'Reading fixture', source: 'youtube',
    content_kind: 'article', publish_at: 0, read_at: null, starred_at: null },
  content: [{ type: 'heading', text: 'Section', level: 2 }, { type: 'paragraph', text: 'Reading content.' }],
  content_status: 'ready', content_updated_at: null, images: [],
};

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
    viewportWidth = 1440;
    mediaListeners.clear();
    vi.stubGlobal('IntersectionObserver', StubIntersectionObserver);
    vi.stubGlobal('matchMedia', stubMatchMedia);
    vi.stubGlobal('localStorage', stubStorage());
    apiGetMock.mockImplementation(async (path: string) => {
      if (path === '/api/source') return { sources: [{ source: 'youtube', channels: 1 }, { source: 'feed', channels: 2 }] };
      if (path === '/api/article/8101708') return articlePayload;
      if (path.startsWith('/api/group')) return { groups: [] };
      if (path.startsWith('/api/article')) {
        return { articles: [], total: 0, item_show_type_facets: [] };
      }
      return {};
    });
  });

  afterEach(() => {
    cleanup();
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

  it('keeps source switching available in the tablet reader', async () => {
    viewportWidth = 1024;
    const { container } = renderPage('/articles?article=8101708&source=youtube');
    await screen.findByText('Reading fixture');
    expect(container.querySelector('.article-layout')?.classList.contains('is-mobile-reading')).toBe(true);
    expect((screen.getByRole('combobox', { name: '来源' }) as HTMLSelectElement).value).toBe('youtube');
    fireEvent.change(screen.getByRole('combobox', { name: '来源' }), { target: { value: 'feed' } });
    await waitFor(() => expect(container.querySelector('.article-layout')?.classList.contains('is-mobile-reading')).toBe(false));
    expect(container.querySelector('.article-preview-title')).toBeNull();
    expect((screen.getByRole('combobox', { name: '来源' }) as HTMLSelectElement).value).toBe('feed');
  });

  it('replaces the hidden wide rail with a source selector when the list is collapsed', async () => {
    viewportWidth = 2000;
    const { container } = renderPage('/articles?source=youtube');
    await screen.findByRole('button', { name: 'YouTube · 1 个订阅' });
    expect(screen.queryByRole('combobox', { name: '来源' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '收起列表' }));
    expect(container.querySelector('.article-view-rail')).toBeNull();
    expect((screen.getByRole('combobox', { name: '来源' }) as HTMLSelectElement).value).toBe('youtube');
    fireEvent.click(screen.getByRole('button', { name: '展开列表' }));
    expect(container.querySelector('.article-view-rail')).not.toBeNull();
    expect(apiGetMock.mock.calls.filter(([path]) => path === '/api/source')).toHaveLength(1);
  });

  it('returns to a visible list after collapsing on desktop and resizing to a phone', async () => {
    const { container } = renderPage('/articles?article=8101708');
    await screen.findByText('Reading fixture');
    fireEvent.click(screen.getByRole('button', { name: '收起列表' }));
    expect(container.querySelector('.article-layout')?.classList.contains('is-collapsed')).toBe(true);
    resizeViewport(390);
    expect(container.querySelector('.article-layout')?.classList.contains('is-collapsed')).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: zhCN['articles.backToList'] }));
    expect(container.querySelector('.article-layout')?.classList.contains('is-mobile-reading')).toBe(false);
    expect(container.querySelector('.article-layout')?.classList.contains('is-collapsed')).toBe(false);
    expect(apiGetMock.mock.calls.filter(([path]) => path === '/api/article/8101708')).toHaveLength(1);
  });

  it('opens filters and the list together from a collapsed reader', async () => {
    const { container } = renderPage();
    fireEvent.click(screen.getByRole('button', { name: '收起列表' }));
    fireEvent.click(screen.getByRole('button', { name: zhCN['articles.toggleFilters'] }));
    expect(container.querySelector('.article-layout')?.classList.contains('is-collapsed')).toBe(false);
    expect(container.querySelector('#article-filter-drawer')?.classList.contains('is-open')).toBe(true);
    await waitFor(() => expect(apiGetMock).toHaveBeenCalled());
  });

  it('preserves an unknown selected source and offers retry when source loading fails', async () => {
    const original = apiGetMock.getMockImplementation()!;
    apiGetMock.mockImplementation((path: string) => path === '/api/source'
      ? Promise.reject(new Error('offline')) : original(path));
    renderPage('/articles?source=mastodon');
    await screen.findByRole('button', { name: '重试来源' });
    expect((screen.getByRole('combobox', { name: '来源' }) as HTMLSelectElement).value).toBe('mastodon');
    apiGetMock.mockImplementation(original);
    fireEvent.click(screen.getByRole('button', { name: '重试来源' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '重试来源' })).toBeNull());
    expect(screen.getByRole('option', { name: 'YouTube' })).toBeTruthy();
  });


  it('switches side panels to overlay based on the actual reading width', async () => {
    let onResize!: ResizeObserverCallback;
    const disconnect = vi.fn();
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: ResizeObserverCallback) { onResize = callback; }
      observe() {}
      disconnect = disconnect;
    });
    const { container, unmount } = renderPage('/articles?article=8101708');
    await screen.findByText('Reading fixture');
    const preview = container.querySelector('#article-preview')!;
    const resize = (width: number) => act(() => onResize([
      { target: preview, contentRect: { width } } as ResizeObserverEntry,
    ], {} as ResizeObserver));
    resize(600);
    expect(preview.classList.contains('is-overlay-panel')).toBe(true);
    resize(900);
    expect(preview.classList.contains('is-overlay-panel')).toBe(false);
    unmount();
    expect(disconnect).toHaveBeenCalled();
  });

});
