import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ActiveTaskPanel } from './ActiveTaskPanel';
import zhCN from '../../i18n/zh-CN.json';

const state = vi.hoisted(() => vi.fn());
vi.mock('../../store/settings', () => ({ useSettingsState: state }));
vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('../../i18n', () => ({ useI18n: () => ({ t: (key: string, fallback?: string) => zhCN[key as keyof typeof zhCN] || fallback || key }) }));
const job = { task_id: 'one', status: 'queued', trigger_type: 'backfill', created_at: '', accounts: [], accounts_total: 0 };

describe('active discovery task', () => {
  it('recognizes queued jobs instead of showing no active task', () => {
    state.mockReturnValue({ state: { syncTasks: [job] } });
    render(<ActiveTaskPanel />);
    expect(screen.getByText('历史列表补全')).toBeTruthy();
    expect(screen.getByRole('button', { name: '暂停历史补全' })).toBeTruthy();
    expect(screen.queryByText(zhCN['sync.activeEmpty'])).toBeNull();
  });
  it('keeps cancelling jobs visible and does not offer another cancellation', () => {
    state.mockReturnValue({ state: { syncTasks: [{ ...job, status: 'cancelling' }] } });
    render(<ActiveTaskPanel />);
    expect(screen.getByText(zhCN['sync.cancelling'])).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.queryByText(zhCN['sync.activeEmpty'])).toBeNull();
  });
});
