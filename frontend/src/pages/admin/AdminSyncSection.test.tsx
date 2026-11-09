import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AdminSyncSection } from './AdminSyncSection';
import zhCN from '../../i18n/zh-CN.json';

const get = vi.hoisted(() => vi.fn());
vi.mock('../../api', () => ({ apiGet: get }));
vi.mock('../../hooks/useSyncRun', () => ({ useSyncRun: () => ({ runSync: vi.fn() }) }));
vi.mock('../../i18n', () => ({ useI18n: () => ({ t: (key: string, fallback?: string) => zhCN[key as keyof typeof zhCN] || fallback || key }) }));

describe('source task visibility', () => {
  afterEach(() => { vi.useRealTimers(); get.mockClear(); });
  it('queries active jobs independently of recent history and labels enqueued counts', async () => {
    get.mockImplementation(async (url: string) => ({ tasks: url.includes('active=true') ? [{
      task_id: 'active', status: 'running', trigger_type: 'backfill', created_at: '2026-11-09T00:00:00Z',
      accounts: [], accounts_total: 1, accounts_done: 0, total_saved: 1187,
      current_account: { nickname: '郭延娇的公众号' }, last_log: '第 59 页',
    }] : [] }));
    render(<AdminSyncSection source="weixin" />);
    expect(await screen.findByText(/历史列表补全/)).toBeTruthy();
    expect(screen.getByText(/新入队 1187 篇/)).toBeTruthy();
    expect(screen.getByText(/郭延娇的公众号/)).toBeTruthy();
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    const urls = get.mock.calls.map(([url]) => new URL(String(url), 'http://localhost'));
    expect(urls.map((url) => url.searchParams.get('active'))).toEqual(['false', 'true']);
    expect(urls.every((url) => url.searchParams.get('source') === 'weixin')).toBe(true);
  });
  it('shows active progress while history is still loading', async () => {
    let resolveHistory!: (value: unknown) => void;
    get.mockImplementation((url: string) => url.includes('active=true')
      ? Promise.resolve({ tasks: [{ task_id: 'live', status: 'running', trigger_type: 'backfill',
        created_at: '2026-11-09T00:00:00Z', accounts_total: 1, accounts_done: 0,
        current_account: { nickname: 'Active account' } }] })
      : new Promise((resolve) => { resolveHistory = resolve; }));
    render(<AdminSyncSection source="weixin" />);
    expect(await screen.findByText(/Active account/)).toBeTruthy();
    expect(screen.queryByText('加载中…')).toBeNull();
    await act(async () => resolveHistory({ tasks: [{ task_id: 'old', status: 'success',
      trigger_type: 'scheduled', created_at: '2026-11-08T00:00:00Z', accounts_total: 0 }] }));
    expect(screen.getByText(/Active account/)).toBeTruthy();
    expect(screen.getByText(/定时/)).toBeTruthy();
  });

  it('clears the previous source when switching to all-source history', async () => {
    get.mockImplementation(async (url: string) => ({ tasks: url.includes('active=true') ? [{
      task_id: 'active', status: 'running', trigger_type: 'backfill', created_at: '2026-11-09T00:00:00Z',
      accounts_total: 1, current_account: { nickname: 'Previous source' },
    }] : [] }));
    const view = render(<AdminSyncSection source="weixin" />);
    expect(await screen.findByText(/Previous source/)).toBeTruthy();
    view.rerender(<AdminSyncSection />);
    expect(await screen.findByText(zhCN['sync.historyEmpty'])).toBeTruthy();
    expect(screen.queryByText(/Previous source/)).toBeNull();
  });

  it('polls active tasks frequently and refreshes history when a task ends', async () => {
    vi.useFakeTimers();
    let running = true;
    get.mockImplementation(async (url: string) => ({ tasks: url.includes('active=true') && running ? [{
      task_id: 'live', status: 'running', trigger_type: 'scheduled', created_at: '', accounts_total: 1,
    }] : [] }));
    render(<AdminSyncSection source="weixin" />);
    await act(() => vi.advanceTimersByTimeAsync(0));
    const histories = () => get.mock.calls.filter(([url]) => String(url).includes('active=false')).length;
    expect(histories()).toBe(1);
    await act(() => vi.advanceTimersByTimeAsync(15000));
    expect(histories()).toBe(1);
    running = false;
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(histories()).toBe(2);
  });
  it('keeps history visible after a failed refresh and supports retry', async () => {
    get.mockResolvedValueOnce({ tasks: [{ task_id: 'old', status: 'success', trigger_type: 'scheduled',
      created_at: '', accounts_total: 0 }] });
    render(<AdminSyncSection />);
    expect(await screen.findByText(/定时/)).toBeTruthy();
    get.mockRejectedValueOnce(new Error('Request timed out'));
    await act(async () => window.dispatchEvent(new Event('hippo:refresh')));
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByText(/定时/)).toBeTruthy();
    get.mockResolvedValueOnce({ tasks: [] });
    fireEvent.click(screen.getByRole('button', { name: zhCN['common.retry'] }));
    expect(await screen.findByText(zhCN['sync.historyEmpty'])).toBeTruthy();
  });

});
