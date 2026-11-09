import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { AccountSync } from '../store/shared';
import { AccountSyncStatus } from './AccountSyncStatus';
import zhCN from '../i18n/zh-CN.json';

const send = vi.hoisted(() => vi.fn());
vi.mock('../api', () => ({ apiSend: send, isAuthError: () => false }));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('../i18n', () => ({ useI18n: () => ({ t: (key: string, fallback?: string) => zhCN[key as keyof typeof zhCN] || fallback || key }) }));
const base: AccountSync = {
  history: { status: 'complete', task_id: null, error: null },
  articles: { pending: 1112, processing: 2, failed: 3 }, listing: null, last_checked_at: null,
};

describe('account ingestion stages', () => {
  it('shows a completed historical list alongside unfinished and failed bodies', () => {
    render(<AccountSyncStatus sync={base} biz="one" disabled={false} />);
    expect(screen.getByText('历史列表已补全')).toBeTruthy();
    expect(screen.getByText('正文待抓取 1112 篇')).toBeTruthy();
    expect(screen.getByText('正文正在抓取 2 篇')).toBeTruthy();
    expect(screen.getByText('正文抓取失败 3 篇')).toBeTruthy();
    expect(screen.queryByText(zhCN['accounts.lastSyncedEmpty'])).toBeNull();
  });

  it('resumes paused history without restarting completed history', async () => {
    send.mockResolvedValue({ status: 'queued' });
    const { rerender } = render(<AccountSyncStatus sync={{ ...base, history: { ...base.history, status: 'paused' } }} biz="one" disabled={false} />);
    fireEvent.click(screen.getByRole('button', { name: '继续补全历史' }));
    await waitFor(() => expect(send).toHaveBeenCalledWith('/api/account/one/backfill', 'POST', {}));
    rerender(<AccountSyncStatus sync={base} biz="one" disabled={false} />);
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('does not offer a resume for a disabled subscription', () => {
    render(<AccountSyncStatus sync={{ ...base, history: { ...base.history, status: 'paused' } }} biz="one" disabled />);
    expect(screen.queryByRole('button')).toBeNull();
  });
});
