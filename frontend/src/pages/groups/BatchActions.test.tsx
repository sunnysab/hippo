import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { BatchActions } from './BatchActions';
import zhCN from '../../i18n/zh-CN.json';

const mocks = vi.hoisted(() => ({
  state: { accounts: [], groups: [], selectedAccounts: ['one', 'two'] },
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
