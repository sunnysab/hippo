import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AdminSourcePage } from './AdminSourcePage';
import { SourceSyncOverview } from './SourceSyncOverview';

const apiGetMock = vi.fn();
const apiSendMock = vi.fn();
const showToastMock = vi.fn();

vi.mock('../../api', () => ({
  apiGet: (...args: unknown[]) => apiGetMock(...args),
  apiSend: (...args: unknown[]) => apiSendMock(...args),
}));

vi.mock('../../i18n', () => ({
  useI18n: () => ({ t: (key: string, fallback?: string) => fallback || key }),
}));

vi.mock('../../hooks/useToast', () => ({
  useToast: () => ({ showToast: showToastMock }),
}));

const OVERVIEW = {
  media_enabled: true,
  sources: [
    {
      source: 'weixin',
      channels: 264,
      enabled_channels: 264,
      followers: 1,
      last_synced_at: '2026-05-02T12:00:00.000Z',
      credential: null,
      proxy: 'direct',
      backlog: { pending: 12, processing: 1, failed: 0, done: 40 },
    },
    {
      source: 'bilibili',
      channels: 3,
      enabled_channels: 3,
      followers: 1,
      last_synced_at: null,
      credential: { field: 'bilibili_cookie', configured: false },
      proxy: 'direct',
      backlog: null,
    },
  ],
};

const renderPage = (ui: React.ReactElement) =>
  render(<MemoryRouter>{ui}</MemoryRouter>);

describe('AdminSourcePage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiGetMock.mockResolvedValue(OVERVIEW);
  });

  it('lists every source the backend reports, with its credential state', async () => {
    renderPage(<AdminSourcePage />);

    expect(await screen.findByText('微信公众号')).toBeTruthy();
    expect(screen.getByText('哔哩哔哩')).toBeTruthy();
    // 来源清单来自后端，未配置的凭证要说出来，不需要凭证的则明说。
    expect(screen.getByText('未配置')).toBeTruthy();
    expect(screen.getByText('不需要')).toBeTruthy();
  });

  it('picks the source when its sync button is pressed, instead of the whole instance', async () => {
    apiSendMock.mockResolvedValue({ task_id: 'task-1', status: 'success' });
    renderPage(<AdminSourcePage />);

    const rows = await screen.findAllByRole('button', { name: '运行' });
    fireEvent.click(rows[0]);

    await waitFor(() =>
      expect(apiSendMock).toHaveBeenCalledWith(
        '/api/settings/admin/run',
        'POST',
        { source: 'weixin' },
        expect.anything(),
      ),
    );
  });

  it('will not offer to sync a source nobody follows, even when the catalogue has rows', async () => {
    // 目录里有一条、但没人订阅：点下去只会得到 404，所以按钮要禁用。
    apiGetMock.mockResolvedValue({
      media_enabled: true,
      sources: [{ ...OVERVIEW.sources[1], channels: 1, followers: 0 }],
    });
    renderPage(<AdminSourcePage />);

    const button = (await screen.findAllByRole('button', { name: '运行' }))
      .find((element) => element.closest('tr'))!;
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it('shows the enabled Caixin channel range and its distinct subscribers', async () => {
    apiGetMock.mockResolvedValue({
      media_enabled: true,
      sources: [{
        ...OVERVIEW.sources[1],
        source: 'caixin',
        channels: 8,
        enabled_channels: 3,
        followers: 2,
      }],
    });
    renderPage(<AdminSourcePage />);

    expect(await screen.findByText(/已启用 3 \/ 8 个板块/)).toBeTruthy();
    expect(screen.getByText(/2 人订阅/)).toBeTruthy();
  });

  it('shows a load failure and retries it', async () => {
    apiGetMock.mockRejectedValueOnce(new Error('boom'));
    renderPage(<AdminSourcePage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }));

    expect(await screen.findByText('微信公众号')).toBeTruthy();
  });
});

describe('SourceSyncOverview', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiGetMock.mockResolvedValue(OVERVIEW);
  });

  it('counts pending and in-flight work together, and links to the source', async () => {
    renderPage(<SourceSyncOverview />);

    // 待处理 = pending + processing，用户关心的是「还有多少没做完」。
    expect(await screen.findByText('待处理 13')).toBeTruthy();
  });
});
