import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import { emitRefresh } from '../../utils/events';
import type { ArticleFiltersState } from './filtering';
import { ViewRail } from './ViewRail';

const apiGetMock = vi.fn();

vi.mock('../../api', () => ({
  apiGet: (path: string) => apiGetMock(path),
}));

const filters = (overrides: Partial<ArticleFiltersState> = {}): ArticleFiltersState => ({
  groupId: '',
  accountBiz: '',
  itemShowType: '',
  source: '',
  read: '',
  starred: '',
  search: '',
  sort: 'publish_at_desc',
  ...overrides,
});

const groups = [
  { value: '1', label: '科技', count: 3 },
  { value: '2', label: '金融', count: 0 },
];

const renderRail = (state: Partial<ArticleFiltersState> = {}) => {
  const onChange = vi.fn();
  const view = render(
    <I18nProvider>
      <ViewRail filters={filters(state)} groupOptions={groups} onChange={onChange} />
    </I18nProvider>,
  );
  return { onChange, ...view };
};

describe('ViewRail', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiGetMock.mockResolvedValue({
      sources: [
        { source: 'weixin', channels: 264 },
        { source: 'bilibili', channels: 3 },
      ],
    });
  });

  it('marks the active view and switches to another one', () => {
    const { onChange } = renderRail({ read: 'unread' });

    expect(screen.getByText('未读').getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByText('全部').getAttribute('aria-pressed')).toBe('false');

    fireEvent.click(screen.getByText('收藏'));

    expect(onChange).toHaveBeenCalledWith({ read: '', starred: 'true' });
  });

  it('offers the sources the reader actually follows, with channel counts', async () => {
    const { onChange } = renderRail();

    await waitFor(() => expect(screen.getByText('哔哩哔哩')).toBeTruthy());
    expect(screen.getByText('264')).toBeTruthy();

    fireEvent.click(screen.getByText('哔哩哔哩'));

    expect(onChange).toHaveBeenCalledWith({ source: 'bilibili' });
  });

  it('shows an unregistered source by its identifier instead of hiding it', async () => {
    apiGetMock.mockResolvedValue({ sources: [{ source: 'mastodon', channels: 1 }] });

    renderRail();

    await waitFor(() => expect(screen.getByText('mastodon')).toBeTruthy());
  });

  it('clears a source filter through the "all sources" entry', async () => {
    const { onChange } = renderRail({ source: 'bilibili' });

    await waitFor(() => expect(screen.getByText('全部来源')).toBeTruthy());
    fireEvent.click(screen.getByText('全部来源'));

    expect(onChange).toHaveBeenCalledWith({ source: '' });
  });

  it('lists groups and picks one', () => {
    const { onChange } = renderRail();

    fireEvent.click(screen.getByText('金融'));

    expect(onChange).toHaveBeenCalledWith({ groupId: '2' });
  });

  it('shows how many subscriptions each group holds, zero included', () => {
    renderRail();

    expect(screen.getByText('3')).toBeTruthy();
    expect(screen.getByText('0')).toBeTruthy();
  });

  it('names the subscription count for screen readers', () => {
    renderRail();

    expect(screen.getByRole('button', { name: '金融 · 0 个订阅' })).toBeTruthy();
  });

  it('names the source count as subscriptions, not as channels', async () => {
    renderRail();

    await waitFor(() => expect(screen.getByRole('button', { name: '哔哩哔哩 · 3 个订阅' })).toBeTruthy());
  });

  it('refreshes the source counts when subscriptions change', async () => {
    renderRail();
    await waitFor(() => expect(screen.getByText('哔哩哔哩')).toBeTruthy());

    apiGetMock.mockResolvedValue({ sources: [{ source: 'bilibili', channels: 4 }] });
    act(() => {
      emitRefresh();
    });

    await waitFor(() => expect(screen.getByText('4')).toBeTruthy());
    expect(apiGetMock).toHaveBeenCalledTimes(2);
  });

  it('keeps the newest source answer when a slow one lands after it', async () => {
    let releaseFirst: (payload: unknown) => void = () => {};
    apiGetMock.mockImplementationOnce(() => new Promise((resolve) => {
      releaseFirst = resolve;
    }));
    apiGetMock.mockResolvedValueOnce({ sources: [{ source: 'bilibili', channels: 300 }] });

    // 第一次请求（挂载）还没回来，刷新先落地：旧答案必须被丢掉。
    renderRail();
    act(() => {
      emitRefresh();
    });
    await waitFor(() => expect(screen.getByText('300')).toBeTruthy());

    await act(async () => {
      releaseFirst({ sources: [{ source: 'bilibili', channels: 1 }] });
      await Promise.resolve();
    });

    expect(screen.getByText('300')).toBeTruthy();
  });

  it('still renders when the source list cannot be read', async () => {
    apiGetMock.mockRejectedValue(new Error('offline'));

    const { onChange } = renderRail();

    // 来源读不到只是少一段，视图与分组照旧可用。
    fireEvent.click(screen.getByText('未读'));
    expect(onChange).toHaveBeenCalledWith({ read: 'unread', starred: '' });
    // 等失败的 promise 落定，否则状态更新会在 act() 之外发生。
    await waitFor(() => expect(screen.queryByText('来源')).toBeNull());
  });
});
