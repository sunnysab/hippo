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
    apiSendMock.mockReset().mockResolvedValue({});
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

  it('opens Watch Later independently of the previous source and search', async () => {
    const original = apiGetMock.getMockImplementation()!;
    apiGetMock.mockImplementation((path: string) => path === '/api/watch-later'
      ? Promise.resolve({ articles: [{ ...articlePayload.article, title: 'Queued article', watch_later_at: '2026-05-01' }], total: 1 }) : original(path));
    renderPage('/articles?source=youtube&q=hidden&read=unread');
    fireEvent.click(screen.getByRole('button', { name: zhCN['articles.watchLater'] }));
    await screen.findByText('Queued article');
    expect(apiGetMock).toHaveBeenCalledWith('/api/watch-later');
    expect(screen.queryByRole('searchbox')).toBeNull();
    expect(screen.queryByRole('combobox', { name: '来源' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: zhCN['articles.viewAll'] }));
    await waitFor(() => expect(screen.queryByText('Queued article')).toBeNull());
    expect(screen.getByRole('searchbox')).toHaveProperty('value', '');
  });

  it('shows a retry rather than an empty queue when loading fails', async () => {
    const original = apiGetMock.getMockImplementation()!;
    apiGetMock.mockImplementation((path: string) => path === '/api/watch-later'
      ? Promise.reject(new Error('queue offline')) : original(path));
    renderPage('/articles?view=watch-later');
    await screen.findByRole('alert');
    expect(screen.queryByText(zhCN['articles.watchLaterEmpty'])).toBeNull();
    apiGetMock.mockImplementation((path: string) => path === '/api/watch-later'
      ? Promise.resolve({ articles: [], total: 0 }) : original(path));
    fireEvent.click(screen.getByRole('button', { name: zhCN['common.retry'] }));
    await screen.findByText(zhCN['articles.watchLaterEmpty']);
  });

  it('persists queue order changes and refreshes the saved list', async () => {
    const first = { ...articlePayload.article, title: 'First queued', watch_later_at: '2026-05-01' };
    const second = { ...first, id: 2, title: 'Second queued' };
    let articles = [first, second];
    const original = apiGetMock.getMockImplementation()!;
    apiGetMock.mockImplementation((path: string) => path === '/api/watch-later'
      ? Promise.resolve({ articles, total: articles.length }) : original(path));
    apiSendMock.mockImplementation(async (_path: string, method: string) => {
      if (method === 'PATCH') articles = [second, first];
      return {};
    });
    const { container } = renderPage('/articles?view=watch-later');
    await screen.findByText('Second queued');
    const up = screen.getAllByRole('button', { name: zhCN['articles.watchLaterUp'] });
    expect(up[0]).toHaveProperty('disabled', true);
    fireEvent.click(up[1]);
    await waitFor(() => expect(apiSendMock).toHaveBeenCalledWith('/api/watch-later/2', 'PATCH', { direction: 'up' }));
    await waitFor(() => expect(container.querySelector('.article-title')?.textContent).toContain('Second queued'));
  });

  it('updates queue badges only after success and prevents duplicate writes', async () => {
    const original = apiGetMock.getMockImplementation()!;
    apiGetMock.mockImplementation((path: string) => path.startsWith('/api/article?')
      ? Promise.resolve({ articles: [{ ...articlePayload.article, title: 'Save this' }], total: 1 }) : original(path));
    let finish!: (result: { watch_later_at: string }) => void;
    apiSendMock.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    renderPage();
    await screen.findByText('Save this');
    const button = screen.getAllByRole('button', { name: zhCN['articles.addWatchLater'] }).find((element) => !element.hasAttribute('disabled'))!;
    fireEvent.click(button);
    fireEvent.click(button);
    expect(apiSendMock).toHaveBeenCalledTimes(1);
    expect(button.getAttribute('aria-pressed')).toBe('false');
    await act(async () => finish({ watch_later_at: '2026-05-01' }));
    expect(screen.getAllByRole('button', { name: zhCN['articles.removeWatchLater'] }).every((element) => element.getAttribute('aria-pressed') === 'true')).toBe(true);
  });

  it('keeps a failed add unselected', async () => {
    const original = apiGetMock.getMockImplementation()!;
    apiGetMock.mockImplementation((path: string) => path.startsWith('/api/article?')
      ? Promise.resolve({ articles: [{ ...articlePayload.article, title: 'Save this' }], total: 1 }) : original(path));
    apiSendMock.mockRejectedValue(new Error('write failed'));
    renderPage();
    await screen.findByText('Save this');
    fireEvent.click(screen.getAllByRole('button', { name: zhCN['articles.addWatchLater'] }).find((element) => !element.hasAttribute('disabled'))!);
    await screen.findByText('write failed');
    expect(screen.queryByRole('button', { name: zhCN['articles.removeWatchLater'] })).toBeNull();
  });

  const mockMediaQueue = (embeddedAudio = false) => {
    const rows = [101, 102, 103].map((id) => ({ ...articlePayload.article, id, title: `Queue ${id}`,
      content_kind: id === 103 ? 'article' : 'video', watch_later_at: '2026-05-01' }));
    const original = apiGetMock.getMockImplementation()!;
    apiGetMock.mockImplementation((path: string) => {
      if (path === '/api/watch-later' || path.startsWith('/api/article?')) return Promise.resolve({ articles: rows, total: rows.length });
      const row = rows.find((item) => path === `/api/article/${item.id}`);
      if (row) return Promise.resolve({ ...articlePayload, article: row, content: row.content_kind === 'video'
        ? [{ type: 'video', url: `/api/media/${row.id}/video` }] : [{ type: 'paragraph', text: 'Read this before continuing.' },
          ...(embeddedAudio ? [{ type: 'audio', url: `/api/media/${row.id}/audio` }] : [])] });
      return original(path);
    });
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue();
    return { play, rows };
  };

  it('continues media in saved order, then stops on a text article', async () => {
    const { play } = mockMediaQueue();
    const { container } = renderPage('/articles?view=watch-later&article=101');
    await waitFor(() => expect(container.querySelector('video')?.getAttribute('src')).toBe('/api/media/101/video'));
    expect(play).not.toHaveBeenCalled();
    const old = container.querySelector('video')!;
    fireEvent.loadedMetadata(old);
    fireEvent.play(old);
    fireEvent.ended(old);
    await waitFor(() => expect(container.querySelector('video')?.getAttribute('src')).toBe('/api/media/102/video'));
    const second = container.querySelector('video')!;
    Object.defineProperty(second, 'readyState', { configurable: true, value: 1 });
    fireEvent.loadedMetadata(second);
    await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
    fireEvent.ended(old);
    expect(container.querySelector('video')).toBe(second);
    fireEvent.ended(second);
    await screen.findByText('Read this before continuing.');
    expect(container.querySelector('video')).toBeNull();
    expect(play).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: zhCN['articles.watchLaterNext'] })).toHaveProperty('disabled', true);
    expect(apiSendMock.mock.calls.some(([, method]) => method === 'DELETE')).toBe(false);
    play.mockRestore();
  });

  it('never advances after media ends outside Watch Later', async () => {
    const { play } = mockMediaQueue();
    const { container } = renderPage('/articles?article=101');
    await waitFor(() => expect(container.querySelector('video')).not.toBeNull());
    fireEvent.loadedMetadata(container.querySelector('video')!);
    fireEvent.play(container.querySelector('video')!);
    fireEvent.ended(container.querySelector('video')!);
    expect(apiGetMock.mock.calls.some(([path]) => path === '/api/article/102')).toBe(false);
    expect(play).not.toHaveBeenCalled();
    play.mockRestore();
  });

  it('defers an ended item until a pending reorder has saved, then uses the new order', async () => {
    const { play, rows } = mockMediaQueue();
    let finish!: () => void;
    apiSendMock.mockImplementation(async (_path: string, method: string) => {
      if (method === 'PATCH') await new Promise<void>((resolve) => { finish = () => {
        rows.splice(1, 2, rows[2], rows[1]);
        resolve();
      }; });
      return {};
    });
    const { container } = renderPage('/articles?view=watch-later&article=101');
    await waitFor(() => expect(container.querySelector('video')).not.toBeNull());
    await screen.findByText('Queue 103');
    const first = container.querySelector('video')!;
    fireEvent.loadedMetadata(first);
    fireEvent.play(first);
    fireEvent.click(screen.getAllByRole('button', { name: zhCN['articles.watchLaterUp'] })[2]);
    fireEvent.ended(first);
    expect(apiGetMock.mock.calls.some(([path]) => path === '/api/article/102')).toBe(false);
    await act(async () => finish());
    await screen.findByText('Read this before continuing.');
    expect(apiGetMock.mock.calls.some(([path]) => path === '/api/article/103')).toBe(true);
    expect(apiGetMock.mock.calls.some(([path]) => path === '/api/article/102')).toBe(false);
    play.mockRestore();
  });

  it('retains an ended event while the initial queue is still loading', async () => {
    const { play, rows } = mockMediaQueue();
    const original = apiGetMock.getMockImplementation()!;
    let finish!: (result: unknown) => void;
    apiGetMock.mockImplementation((path: string) => path === '/api/watch-later'
      ? new Promise((resolve) => { finish = resolve; }) : original(path));
    const { container } = renderPage('/articles?view=watch-later&article=101');
    await waitFor(() => expect(container.querySelector('video')).not.toBeNull());
    fireEvent.loadedMetadata(container.querySelector('video')!);
    fireEvent.play(container.querySelector('video')!);
    fireEvent.ended(container.querySelector('video')!);
    await act(async () => finish({ articles: rows, total: rows.length }));
    await waitFor(() => expect(container.querySelector('video')?.getAttribute('src')).toBe('/api/media/102/video'));
    play.mockRestore();
  });

  it('cancels a deferred end when a phone reader returns to the list', async () => {
    viewportWidth = 390;
    const { play, rows } = mockMediaQueue();
    const original = apiGetMock.getMockImplementation()!;
    let finish!: (result: unknown) => void;
    apiGetMock.mockImplementation((path: string) => path === '/api/watch-later'
      ? new Promise((resolve) => { finish = resolve; }) : original(path));
    const { container } = renderPage('/articles?view=watch-later&article=101');
    await waitFor(() => expect(container.querySelector('video')).not.toBeNull());
    fireEvent.loadedMetadata(container.querySelector('video')!);
    fireEvent.play(container.querySelector('video')!);
    fireEvent.ended(container.querySelector('video')!);
    fireEvent.click(screen.getByRole('button', { name: zhCN['articles.backToList'] }));
    await act(async () => finish({ articles: rows, total: rows.length }));
    expect(apiGetMock.mock.calls.some(([path]) => path === '/api/article/102')).toBe(false);
    expect(container.querySelector('.article-layout')?.classList.contains('is-mobile-reading')).toBe(false);
    expect(play).not.toHaveBeenCalled();
    play.mockRestore();
  });

  it('ignores media ending while a phone reader is showing the list', async () => {
    viewportWidth = 390;
    const { play } = mockMediaQueue();
    const { container } = renderPage('/articles?view=watch-later&article=101');
    await waitFor(() => expect(container.querySelector('video')).not.toBeNull());
    const player = container.querySelector('video')!;
    fireEvent.loadedMetadata(player);
    fireEvent.play(player);
    fireEvent.click(screen.getByRole('button', { name: zhCN['articles.backToList'] }));
    fireEvent.ended(player);
    await act(async () => {});
    expect(apiGetMock.mock.calls.some(([path]) => path === '/api/article/102')).toBe(false);
    expect(container.querySelector('.article-layout')?.classList.contains('is-mobile-reading')).toBe(false);
    play.mockRestore();
  });

  it('does not reopen the phone reader when an automatic next-item request finishes after returning', async () => {
    viewportWidth = 390;
    const { play } = mockMediaQueue();
    const original = apiGetMock.getMockImplementation()!;
    let finish!: () => Promise<void>;
    apiGetMock.mockImplementation((path: string) => path === '/api/article/102'
      ? new Promise((resolve) => { finish = async () => resolve(await original(path)); }) : original(path));
    const { container } = renderPage('/articles?view=watch-later&article=101');
    await waitFor(() => expect(container.querySelector('video')).not.toBeNull());
    fireEvent.loadedMetadata(container.querySelector('video')!);
    fireEvent.play(container.querySelector('video')!);
    fireEvent.ended(container.querySelector('video')!);
    await waitFor(() => expect(apiGetMock.mock.calls.some(([path]) => path === '/api/article/102')).toBe(true));
    fireEvent.click(screen.getByRole('button', { name: zhCN['articles.backToList'] }));
    await act(async () => finish());
    expect(container.querySelector('.article-layout')?.classList.contains('is-mobile-reading')).toBe(false);
    expect(play).not.toHaveBeenCalled();
    play.mockRestore();
  });

  it('stops on an ordinary article even if it embeds playable audio', async () => {
    const { play, rows } = mockMediaQueue(true);
    rows[1].content_kind = 'article';
    rows[2].content_kind = 'video';
    const { container } = renderPage('/articles?view=watch-later&article=101');
    await waitFor(() => expect(container.querySelector('video')).not.toBeNull());
    fireEvent.loadedMetadata(container.querySelector('video')!);
    fireEvent.play(container.querySelector('video')!);
    fireEvent.ended(container.querySelector('video')!);
    await screen.findByText('Read this before continuing.');
    const audio = container.querySelector('audio')!;
    Object.defineProperty(audio, 'readyState', { configurable: true, value: 1 });
    fireEvent.loadedMetadata(audio);
    fireEvent.play(audio);
    fireEvent.ended(audio);
    expect(play).not.toHaveBeenCalled();
    expect(apiGetMock.mock.calls.some(([path]) => path === '/api/article/103')).toBe(false);
    play.mockRestore();
  });

});
