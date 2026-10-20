import { act, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
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
});
