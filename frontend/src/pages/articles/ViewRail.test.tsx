import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import type { ArticleFiltersState } from './filtering';
import { ViewRail } from './ViewRail';

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

const renderRail = (state: Partial<ArticleFiltersState> = {}, sources = [
  { source: 'weixin', channels: 264 }, { source: 'bilibili', channels: 3 },
]) => {
  const onChange = vi.fn();
  const view = render(
    <I18nProvider>
      <ViewRail sources={sources} filters={filters(state)} groupOptions={groups} onChange={onChange} />
    </I18nProvider>,
  );
  return { onChange, ...view };
};

describe('ViewRail', () => {
  it('marks the active view and switches to another one', () => {
    const { onChange } = renderRail({ read: 'unread' });

    expect(screen.getByText('未读').getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByText('全部').getAttribute('aria-pressed')).toBe('false');

    fireEvent.click(screen.getByText('收藏'));

    expect(onChange).toHaveBeenCalledWith({ read: '', starred: 'true' });
  });

  it('offers the sources the reader actually follows, with channel counts', () => {
    const { onChange } = renderRail();

    expect(screen.getByText('哔哩哔哩')).toBeTruthy();
    expect(screen.getByText('264')).toBeTruthy();

    fireEvent.click(screen.getByText('哔哩哔哩'));

    expect(onChange).toHaveBeenCalledWith({ source: 'bilibili' });
  });

  it('shows an unregistered source by its identifier instead of hiding it', () => {
    renderRail({}, [{ source: 'mastodon', channels: 1 }]);
    expect(screen.getByText('mastodon')).toBeTruthy();
  });

  it('clears a source filter through the "all sources" entry', () => {
    const { onChange } = renderRail({ source: 'bilibili' });

    expect(screen.getByText('全部来源')).toBeTruthy();
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

    expect(screen.getByRole('button', { name: '科技 · 3 个订阅' }).textContent).toContain('3');
    expect(screen.getByText('0')).toBeTruthy();
  });

  it('names the subscription count for screen readers', () => {
    renderRail();

    expect(screen.getByRole('button', { name: '金融 · 0 个订阅' })).toBeTruthy();
  });

  it('names the source count as subscriptions, not as channels', () => {
    renderRail();

    expect(screen.getByRole('button', { name: '哔哩哔哩 · 3 个订阅' })).toBeTruthy();
  });

  it('keeps views and groups usable without source choices', () => {
    const { onChange } = renderRail({}, []);
    fireEvent.click(screen.getByText('未读'));
    expect(onChange).toHaveBeenCalledWith({ read: 'unread', starred: '' });
    expect(screen.queryByText('来源')).toBeNull();
  });
});
