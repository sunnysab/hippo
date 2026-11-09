import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { AdminSyncSection } from './AdminSyncSection';
import zhCN from '../../i18n/zh-CN.json';

const get = vi.hoisted(() => vi.fn());
vi.mock('../../api', () => ({ apiGet: get }));
vi.mock('../../hooks/useSyncRun', () => ({ useSyncRun: () => ({ runSync: vi.fn() }) }));
vi.mock('../../i18n', () => ({ useI18n: () => ({ t: (key: string, fallback?: string) => zhCN[key as keyof typeof zhCN] || fallback || key }) }));

describe('source task visibility', () => {
  it('queries active jobs independently of recent history and labels enqueued counts', async () => {
    get.mockImplementation(async (url: string) => ({ tasks: url.includes('active=true') ? [{
      task_id: 'active', status: 'running', trigger_type: 'backfill', created_at: '2026-11-09T00:00:00Z',
      accounts: [], accounts_total: 1, accounts_done: 0, report: { total_saved: 1187 },
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
});
