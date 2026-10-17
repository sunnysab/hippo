import { useEffect } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../api';
import { ArticlesProvider, useArticlesState, type Article, type ArticlesAction } from '../../store/articles';
import { ToastProvider } from '../../hooks/useToast';
import { ArticleList } from './ArticleList';

const article: Article = {
  id: 1,
  biz: 'Mz123',
  article_id: 'a1',
  title: 'First article',
  item_show_type: null,
  author: '',
  digest: '',
  cover: '',
  link: '',
  source_url: '',
  publish_at: 0,
  created_at: '',
  account_nickname: 'Acme Daily',
  account_alias: '',
  account_avatar: '',
  account_avatar_url: '',
  group_id: 1,
  group_name: '',
  image_id: null,
  source: 'weixin',
  read_at: null,
  starred_at: null,
};

let storeDispatch: ((action: ArticlesAction) => void) | null = null;

/** Hands the test a live store dispatch; the production provider stays untouched. */
function StoreBridge() {
  const { dispatch } = useArticlesState();
  useEffect(() => { storeDispatch = dispatch; }, [dispatch]);
  return null;
}

/** Seeds the same guard flags the page produces, through real store actions. */
function seed(options: { hasMore?: boolean; loading?: boolean } = {}) {
  act(() => {
    storeDispatch!({ type: 'SET_ARTICLES', articles: [article], reset: true });
    storeDispatch!({ type: 'SET_PAGE', page: 1, hasMore: options.hasMore ?? true });
    storeDispatch!({ type: 'SET_LOADING', loading: options.loading ?? false });
  });
}

class StubIntersectionObserver {
  static instances: StubIntersectionObserver[] = [];

  readonly targets: Element[] = [];
  readonly disconnect = vi.fn();

  private readonly callback: IntersectionObserverCallback;

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback;
    StubIntersectionObserver.instances.push(this);
  }

  observe(target: Element) {
    this.targets.push(target);
  }

  trigger(isIntersecting = true) {
    this.callback(
      this.targets.map((target) => ({ isIntersecting, target })) as IntersectionObserverEntry[],
      this as unknown as IntersectionObserver,
    );
  }
}

function lastObserver(): StubIntersectionObserver {
  const observer = StubIntersectionObserver.instances[StubIntersectionObserver.instances.length - 1];
  if (!observer) throw new Error('expected an IntersectionObserver to have been created');
  return observer;
}

function renderList(
  options: { hasMore?: boolean; loading?: boolean; onLoadMore?: () => Promise<void> } = {},
) {
  const onLoadMore = options.onLoadMore ?? vi.fn().mockResolvedValue(undefined);
  const view = render(
    <ToastProvider>
      <ArticlesProvider>
        <StoreBridge />
        <ArticleList onSelect={vi.fn().mockResolvedValue(undefined)} onLoadMore={onLoadMore} />
      </ArticlesProvider>
    </ToastProvider>,
  );
  seed({ hasMore: options.hasMore, loading: options.loading });
  return { ...view, onLoadMore };
}

describe('ArticleList pagination', () => {
  beforeEach(() => {
    storeDispatch = null;
    StubIntersectionObserver.instances = [];
    vi.stubGlobal('IntersectionObserver', StubIntersectionObserver);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('loads more when the bottom sentinel comes into view', async () => {
    const { onLoadMore, container } = renderList();

    const observer = lastObserver();
    // The button itself is observed; no extra sentinel element is rendered.
    expect(observer.targets).toEqual([screen.getByRole('button', { name: 'Load more' })]);

    // On phones the page scrolls, not the list, so the list's own scroll event
    // must no longer drive pagination.
    fireEvent.scroll(container.querySelector('#article-list')!);
    expect(onLoadMore).not.toHaveBeenCalled();

    observer.trigger(true);

    await waitFor(() => expect(onLoadMore).toHaveBeenCalledTimes(1));
  });

  it('loads more from the explicit button', async () => {
    const { onLoadMore } = renderList();

    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));

    await waitFor(() => expect(onLoadMore).toHaveBeenCalledTimes(1));
  });

  it('ignores intersection and clicks while a load is in flight', () => {
    const { onLoadMore } = renderList({ loading: true });

    lastObserver().trigger(true);
    const button = screen.getByRole('button', { name: 'Loading…' }) as HTMLButtonElement;
    fireEvent.click(button);

    expect(button.disabled).toBe(true);
    expect(onLoadMore).not.toHaveBeenCalled();
  });

  it('stops observing and hides the button once the last page is in', () => {
    const { onLoadMore } = renderList({ hasMore: false });

    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
    expect(StubIntersectionObserver.instances).toHaveLength(0);
    expect(onLoadMore).not.toHaveBeenCalled();
  });

  it('moves the observer onto the sentinel after the list grows', () => {
    renderList();
    const first = lastObserver();

    act(() => {
      storeDispatch!({ type: 'SET_ARTICLES', articles: [article, { ...article, id: 2 }], reset: false });
    });

    const second = lastObserver();
    expect(second).not.toBe(first);
    expect(first.disconnect).toHaveBeenCalledTimes(1);
    expect(second.targets).toEqual([screen.getByRole('button', { name: 'Load more' })]);
  });

  it('ignores a stale observer notification after the list grew', async () => {
    const { onLoadMore } = renderList();
    const first = lastObserver();

    act(() => {
      storeDispatch!({ type: 'SET_ARTICLES', articles: [article, { ...article, id: 2 }], reset: false });
    });

    // The replaced observer may still have a notification already queued.
    first.trigger(true);
    expect(onLoadMore).not.toHaveBeenCalled();

    lastObserver().trigger(true);

    await waitFor(() => expect(onLoadMore).toHaveBeenCalledTimes(1));
  });

  it('stops observing once the last page arrives mid-scroll', () => {
    renderList();
    const observer = lastObserver();

    act(() => {
      storeDispatch!({ type: 'SET_PAGE', page: 2, hasMore: false });
    });

    expect(observer.disconnect).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
  });

  it('still paginates by button without IntersectionObserver', async () => {
    vi.stubGlobal('IntersectionObserver', undefined);
    const { onLoadMore } = renderList();

    expect(StubIntersectionObserver.instances).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));

    await waitFor(() => expect(onLoadMore).toHaveBeenCalledTimes(1));
  });

  it('disconnects the observer on unmount', () => {
    const { unmount } = renderList();
    const observer = lastObserver();

    unmount();

    expect(observer.disconnect).toHaveBeenCalledTimes(1);
  });

  it('ignores a queued observer notification after unmount', () => {
    const { unmount, onLoadMore } = renderList();
    const observer = lastObserver();

    unmount();
    observer.trigger(true);

    expect(onLoadMore).not.toHaveBeenCalled();
  });

  it('reports a failed load and lets the reader retry', async () => {
    const onLoadMore = vi.fn()
      .mockRejectedValueOnce(new Error(''))
      .mockResolvedValue(undefined);
    renderList({ onLoadMore });

    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));

    await waitFor(() => expect(screen.getByText('Failed to load more articles.')).not.toBeNull());

    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));

    await waitFor(() => expect(onLoadMore).toHaveBeenCalledTimes(2));
  });

  it('leaves the auth failure to the global handler instead of toasting twice', async () => {
    const onLoadMore = vi.fn().mockRejectedValue(
      new ApiError('Authentication required', { code: 'AUTH_REQUIRED' }),
    );
    renderList({ onLoadMore });

    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));

    await waitFor(() => expect(onLoadMore).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('Failed to load more articles.')).toBeNull();
    expect(document.getElementById('app-toast')?.textContent).toBe('');
  });
});
