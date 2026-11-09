import { act, fireEvent, waitFor, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import zhCN from '../../i18n/zh-CN.json';
import { SourceSubscriptionPanel } from './SourceSubscriptionPanel';

const apiGetMock = vi.fn();

vi.mock('../../api', () => ({
  apiGet: (...args: unknown[]) => apiGetMock(...args),
}));

const account = (biz: string) => ({
  biz,
  nickname: `name-${biz}`,
  alias: null,
  is_disabled: false,
  last_synced_at: null,
  latest_publish_at: null,
  article_count: 0,
});

const page = (from: number, count: number, total: number) => ({
  accounts: Array.from({ length: count }, (_, index) => account(`biz-${from + index}`)),
  total,
});

const renderPanel = (source: string) => render(
  <MemoryRouter>
    <I18nProvider>
      <SourceSubscriptionPanel source={source} />
    </I18nProvider>
  </MemoryRouter>,
);

describe('SourceSubscriptionPanel', () => {
  afterEach(() => vi.useRealTimers());
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

  it('keeps publication and sync times in separate columns', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(new Date('2026-10-20T08:00:00+08:00').getTime());
    apiGetMock.mockResolvedValueOnce({
      accounts: [{
        ...account('youtube:UC123'),
        latest_publish_at: '2026-10-20T01:00:00+08:00',
        last_synced_at: '2026-10-20T05:49:00+08:00',
      }],
      total: 1,
    });

    renderPanel('youtube');

    expect(await screen.findByText(zhCN['time.hoursAgo'].replace('{n}', '7'))).toBeTruthy();
    expect(screen.getByText(zhCN['time.hoursAgo'].replace('{n}', '2'))).toBeTruthy();
    expect(screen.getByRole('columnheader', { name: zhCN['accounts.lastUpdatedLabel'] })).toBeTruthy();
    expect(screen.getByRole('columnheader', { name: zhCN['accounts.lastSyncedLabel'] })).toBeTruthy();
    expect(screen.getByRole('columnheader', { name: '已收录文章' })).toBeTruthy();
    expect(screen.queryByText('已收录 {n} 篇')).toBeNull();
  });

  it('pages the subscription list instead of silently truncating it', async () => {
    apiGetMock
      .mockResolvedValueOnce(page(0, 50, 120))
      .mockResolvedValueOnce(page(50, 50, 120));

    renderPanel('bilibili');

    expect(await screen.findByText('name-biz-0')).toBeTruthy();
    expect(apiGetMock).toHaveBeenCalledWith(
      '/api/account?source=bilibili&page=1&page_size=50',
    );

    fireEvent.click(screen.getByRole('button', { name: zhCN['admin.source.loadMore'] }));

    expect(await screen.findByText('name-biz-50')).toBeTruthy();
    expect(screen.getByText('name-biz-0')).toBeTruthy();
    expect(apiGetMock).toHaveBeenLastCalledWith(
      '/api/account?source=bilibili&page=2&page_size=50',
    );
  });

  it('drops a late answer for the source it was switched away from', async () => {
    let releaseOld: (value: unknown) => void = () => {};
    apiGetMock
      .mockImplementationOnce(() => new Promise((resolve) => { releaseOld = resolve; }))
      .mockResolvedValueOnce({ accounts: [account('from-youtube')], total: 1 });

    const view = renderPanel('bilibili');
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledTimes(1));
    view.rerender(
      <MemoryRouter>
        <I18nProvider>
          <SourceSubscriptionPanel source="youtube" />
        </I18nProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText('name-from-youtube')).toBeTruthy();

    await act(async () => {
      releaseOld({ accounts: [account('from-bilibili')], total: 1 });
      await Promise.resolve();
    });

    expect(screen.queryByText('name-from-bilibili')).toBeNull();
    expect(screen.getByText('name-from-youtube')).toBeTruthy();
  });

  it('surfaces a load failure with a retry', async () => {
    apiGetMock
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce({ accounts: [account('recovered')], total: 1 });

    renderPanel('bilibili');

    expect(await screen.findByText(zhCN['admin.source.subscriptionsFailed'])).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: zhCN['common.retry'] }));

    expect(await screen.findByText('name-recovered')).toBeTruthy();
  });
  it('searches subscriptions beyond the loaded page and resets pagination', async () => {
    apiGetMock
      .mockResolvedValueOnce(page(0, 50, 120))
      .mockResolvedValueOnce({ accounts: [account('beyond-page')], total: 1 })
      .mockResolvedValueOnce(page(0, 50, 120));
    renderPanel('weixin');
    await screen.findByText('name-biz-0');
    const input = screen.getByRole('searchbox', { name: '搜索订阅' });
    fireEvent.change(input, { target: { value: '郭延娇' } });
    expect(await screen.findByText('name-beyond-page')).toBeTruthy();
    const url = new URL(apiGetMock.mock.calls.at(-1)![0], 'http://hippo.test');
    expect(url.searchParams.get('q')).toBe('郭延娇');
    expect(url.searchParams.get('page')).toBe('1');
    expect(url.searchParams.get('source')).toBe('weixin');
    expect(screen.queryByRole('button', { name: zhCN['admin.source.loadMore'] })).toBeNull();
    fireEvent.change(input, { target: { value: '' } });
    expect(await screen.findByText('name-biz-0')).toBeTruthy();
    expect(apiGetMock).toHaveBeenLastCalledWith('/api/account?source=weixin&page=1&page_size=50');
  });

  it('ignores a late search response after the query changes', async () => {
    let resolveOld!: (value: unknown) => void;
    apiGetMock
      .mockResolvedValueOnce(page(0, 1, 1))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce({ accounts: [account('new-match')], total: 1 });
    renderPanel('weixin');
    await screen.findByText('name-biz-0');
    const input = screen.getByRole('searchbox');
    fireEvent.change(input, { target: { value: 'old' } });
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledTimes(2));
    fireEvent.change(input, { target: { value: 'new' } });
    expect(await screen.findByText('name-new-match')).toBeTruthy();
    await act(async () => resolveOld({ accounts: [account('old-match')], total: 1 }));
    expect(screen.queryByText('name-old-match')).toBeNull();
  });

  it('keeps the search scope while polling', async () => {
    vi.useFakeTimers();
    apiGetMock.mockResolvedValue({ accounts: [account('one')], total: 1 });
    const view = renderPanel('weixin');
    await act(() => vi.advanceTimersByTimeAsync(0));
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'one' } });
    await act(() => vi.advanceTimersByTimeAsync(250));
    apiGetMock.mockClear();
    await act(() => vi.advanceTimersByTimeAsync(10000));
    const url = new URL(apiGetMock.mock.calls.at(-1)![0], 'http://hippo.test');
    expect(url.searchParams.get('q')).toBe('one');
    view.unmount();
  });

});
