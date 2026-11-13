import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import type { ArticleFiltersState } from './filtering';
import type { ArticleSource } from '../../hooks/useArticleSources';
import { ViewRail } from './ViewRail';

const filters = (overrides: Partial<ArticleFiltersState> = {}): ArticleFiltersState => ({
  groupId: '',
  accountBiz: '',
  itemShowType: '',
  topic: '',
  eventId: '',
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

const renderRail = (state: Partial<ArticleFiltersState> = {}, sources: ArticleSource[] = [
  { source: 'weixin', channels: 264 }, { source: 'bilibili', channels: 3 },
]) => {
  const onChange = vi.fn();
  const onChooseCaixinChannels = vi.fn();
  const view = render(
    <I18nProvider>
      <ViewRail sources={sources} filters={filters(state)} groupOptions={groups} onChange={onChange} onChooseCaixinChannels={onChooseCaixinChannels} />
    </I18nProvider>,
  );
  return { onChange, onChooseCaixinChannels, ...view };
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

  it('opens Caixin discovery from a context menu without changing the source', () => {
    const { onChange, onChooseCaixinChannels } = renderRail({}, [{ source: 'caixin', channels: 0, discoverable: true }]);
    expect(screen.queryByRole('menuitem', { name: '选择财新板块' })).toBeNull();
    fireEvent.contextMenu(screen.getByText('财新'), { clientX: 30, clientY: 40 });
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('menuitem', { name: '选择财新板块' }));
    expect(onChooseCaixinChannels).toHaveBeenCalledOnce();
    expect(screen.queryByRole('menuitem', { name: '选择财新板块' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '财新 · 0 个订阅' }));
    expect(onChange).toHaveBeenCalledWith({ source: 'caixin' });
  });

  it.each([{ key: 'ContextMenu' }, { key: 'F10', shiftKey: true }])('supports keyboard context menus: $key', (event) => {
    renderRail({}, [{ source: 'caixin', channels: 0, discoverable: true }]);
    const trigger = screen.getByRole('button', { name: '财新 · 0 个订阅' });
    trigger.focus();
    fireEvent.keyDown(trigger, event);
    const action = screen.getByRole('menuitem', { name: '选择财新板块' });
    expect(document.activeElement).toBe(action);
    fireEvent.keyDown(action, { key: 'Escape' });
    expect(screen.queryByRole('menuitem', { name: '选择财新板块' })).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it('does not offer discovery when Caixin is unavailable', () => {
    renderRail({}, [{ source: 'caixin', channels: 1, discoverable: false }]);
    fireEvent.contextMenu(screen.getByText('财新'));
    expect(screen.queryByRole('menuitem', { name: '选择财新板块' })).toBeNull();
  });

  it('keeps views and groups usable without source choices', () => {
    const { onChange } = renderRail({}, []);
    fireEvent.click(screen.getByText('未读'));
    expect(onChange).toHaveBeenCalledWith({ read: 'unread', starred: '' });
    expect(screen.queryByText('来源')).toBeNull();
  });
});
