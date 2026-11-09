import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { BatchActions } from './BatchActions';
import zhCN from '../../i18n/zh-CN.json';
import { apiSend } from '../../api';

const mocks = vi.hoisted(() => ({
  state: { accounts: [] as Array<{ biz: string; source: string }>, groups: [], selectedAccounts: ['one', 'two'] },
  toast: vi.fn(),
  auth: vi.fn(),
}));

vi.mock('../../store/groups', () => ({ useGroupsState: () => ({ state: mocks.state, dispatch: vi.fn() }) }));
vi.mock('../../i18n', () => ({
  useI18n: () => ({ t: (key: string, fallback?: string) => zhCN[key as keyof typeof zhCN] || fallback || key }),
}));
vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ showToast: mocks.toast }) }));
vi.mock('../../hooks/useMediaQuery', () => ({ useMediaQuery: () => false }));
vi.mock('../../hooks/useSyncRun', () => ({ useSyncRun: () => ({ runSync: vi.fn() }) }));
vi.mock('../../api', () => ({ apiSend: vi.fn(), handleAuthError: mocks.auth, isAuthError: () => false }));

function openExport() {
  render(<BatchActions />);
  fireEvent.click(screen.getByRole('button', { name: zhCN['accounts.export'] }));
  return within(screen.getByRole('dialog'));
}

describe('account exports', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
    mocks.state.accounts = [];
    vi.stubGlobal('fetch', vi.fn());
    URL.createObjectURL = vi.fn(() => 'blob:export');
    URL.revokeObjectURL = vi.fn();
  });

  it.each(['markdown', 'json'])('downloads all selected accounts as a ZIP with %s files', async (format) => {
    const blob = new Blob(['zip'], { type: 'application/zip' });
    vi.mocked(fetch).mockResolvedValue({ ok: true, status: 200, blob: async () => blob } as Response);
    let filename = '';
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      filename = this.download;
    });
    const dialog = openExport();
    if (format === 'json') fireEvent.click(dialog.getByRole('radio', { name: 'JSON' }));
    fireEvent.click(dialog.getByRole('button', { name: zhCN['accounts.export'] }));

    await waitFor(() => expect(download).toHaveBeenCalledOnce());
    const params = new URL(String(vi.mocked(fetch).mock.calls[0][0]), 'https://hippo.test').searchParams;
    expect(params.getAll('biz')).toEqual(['one', 'two']);
    expect(params.get('format')).toBe(format);
    expect(filename).toBe('hippo-articles.zip');
    expect(URL.createObjectURL).toHaveBeenCalledWith(blob);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:export');
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('handles an expired session without downloading an error response', async () => {
    vi.mocked(fetch).mockResolvedValue({ ok: false, status: 401 } as Response);
    const dialog = openExport();
    fireEvent.click(dialog.getByRole('button', { name: zhCN['accounts.export'] }));

    await waitFor(() => expect(mocks.auth).toHaveBeenCalledOnce());
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });
});

describe('batch update intervals', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.state.selectedAccounts = ['one', 'two'];
    mocks.state.accounts = [{ biz: 'one', source: 'weixin' }, { biz: 'two', source: 'feed' }];
  });

  it('applies an interval only to selected polling subscriptions', async () => {
    render(<BatchActions />);
    fireEvent.change(document.querySelector('#batch-sync-interval')!, { target: { value: '3' } });
    fireEvent.click(document.querySelector('#btn-batch-sync')!);
    await waitFor(() => expect(apiSend).toHaveBeenCalledWith('/api/account/batch', 'POST', {
      biz_list: ['two'], sync_interval_days: 3,
    }));
  });

  it('hides interval controls when only WeChat subscriptions are selected', () => {
    mocks.state.selectedAccounts = ['one'];
    render(<BatchActions />);
    expect(document.querySelector('#batch-sync-interval')).toBeNull();
    expect(document.querySelector('#btn-batch-sync')).toBeNull();
    expect(document.querySelector('#btn-batch-group-sync')).toBeTruthy();
  });
});
