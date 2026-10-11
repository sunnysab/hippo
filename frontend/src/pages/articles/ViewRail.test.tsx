import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
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
  { value: '1', label: '科技' },
  { value: '2', label: '金融' },
];

const renderRail = (state: Partial<ArticleFiltersState> = {}) => {
  const onChange = vi.fn();
  const view = render(<ViewRail filters={filters(state)} groupOptions={groups} onChange={onChange} />);
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
