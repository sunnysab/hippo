import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import zhCN from '../../i18n/zh-CN.json';
import { SourceSubscriptionPanel } from './SourceSubscriptionPanel';

const apiGetMock = vi.fn();
const apiSendMock = vi.fn();
const showToastMock = vi.fn();
const emitRefreshMock = vi.fn();

vi.mock('../../api', () => ({
  apiGet: (...args: unknown[]) => apiGetMock(...args),
  apiSend: (...args: unknown[]) => apiSendMock(...args),
}));
vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ showToast: showToastMock }) }));
vi.mock('../../utils/events', () => ({ emitRefresh: () => emitRefreshMock() }));

const channel = (biz: string, overrides: Record<string, unknown> = {}) => ({
  biz,
  nickname: `name-${biz}`,
  source: biz.split(':')[0],
  alias: null,
  is_disabled: false,
  last_synced_at: '2026-10-20T05:49:00+08:00',
  latest_publish_at: '2026-10-20T01:00:00+08:00',
  article_count: 4,
  subscribers: 2,
  ...overrides,
});

const renderPanel = (source: string, onCollectionStateChange = vi.fn()) => {
  const ui = (nextSource: string) => (
    <MemoryRouter>
      <I18nProvider>
        <SourceSubscriptionPanel
          source={nextSource}
          onCollectionStateChange={onCollectionStateChange}
        />
      </I18nProvider>
    </MemoryRouter>
  );
  const view = render(ui(source));
  return {
    onCollectionStateChange,
    ...view,
    rerenderSource: (nextSource: string) => view.rerender(ui(nextSource)),
  };
};

describe('SourceSubscriptionPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiSendMock.mockResolvedValue({});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('loads the instance catalogue and keeps publication and sync times separate', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(new Date('2026-10-20T08:00:00+08:00').getTime());
    apiGetMock.mockResolvedValue({ channels: [channel('youtube:UC123')] });

    renderPanel('youtube');

    expect(await screen.findByText('name-youtube:UC123')).toBeTruthy();
    expect(apiGetMock).toHaveBeenCalledWith('/api/admin/source/youtube/channel');
    expect(screen.getByRole('columnheader', { name: zhCN['accounts.lastUpdatedLabel'] })).toBeTruthy();
    expect(screen.getByRole('columnheader', { name: zhCN['accounts.lastSyncedLabel'] })).toBeTruthy();
    expect(screen.getByRole('columnheader', { name: zhCN['accounts.articleCountLabel'] })).toBeTruthy();
    expect(screen.queryByRole('checkbox')).toBeNull();
  });

  it('saves the exact Caixin collection range and reports whether any channel remains enabled', async () => {
    const initial = [
      channel('caixin:125'),
      channel('caixin:126', { is_disabled: true }),
    ];
    const updated = [
      channel('caixin:125', { is_disabled: true }),
      channel('caixin:126'),
    ];
    apiGetMock.mockResolvedValueOnce({ channels: initial }).mockResolvedValueOnce({ channels: updated });
    const { onCollectionStateChange } = renderPanel('caixin');

    fireEvent.click(await screen.findByRole('checkbox', { name: '采集 name-caixin:125' }));
    fireEvent.click(screen.getByRole('checkbox', { name: '采集 name-caixin:126' }));
    fireEvent.click(screen.getByRole('button', { name: zhCN['admin.source.saveChannels'] }));

    await waitFor(() => expect(apiSendMock).toHaveBeenCalledTimes(2));
    expect(apiSendMock).toHaveBeenNthCalledWith(
      1,
      '/api/admin/source/caixin/channel',
      'PATCH',
      { biz_list: ['caixin:126'], is_disabled: false },
    );
    expect(apiSendMock).toHaveBeenNthCalledWith(
      2,
      '/api/admin/source/caixin/channel',
      'PATCH',
      { biz_list: ['caixin:125'], is_disabled: true },
    );
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledTimes(2));
    expect(onCollectionStateChange).toHaveBeenLastCalledWith(true);
    expect(emitRefreshMock).toHaveBeenCalled();
  });

  it('drops a late answer from the source it was switched away from', async () => {
    let resolveOld!: (value: unknown) => void;
    apiGetMock
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce({ channels: [channel('youtube:new')] });
    const view = renderPanel('bilibili');
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledTimes(1));
    view.rerenderSource('youtube');

    expect(await screen.findByText('name-youtube:new')).toBeTruthy();
    await act(async () => resolveOld({ channels: [channel('bilibili:old')] }));
    expect(screen.queryByText('name-bilibili:old')).toBeNull();
  });

  it('surfaces a load failure and supports retry', async () => {
    apiGetMock
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce({ channels: [channel('bilibili:recovered')] });
    renderPanel('bilibili');

    expect(await screen.findByText(zhCN['admin.source.channelsFailed'])).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: zhCN['common.retry'] }));

    expect(await screen.findByText('name-bilibili:recovered')).toBeTruthy();
  });
});
