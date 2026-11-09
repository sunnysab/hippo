import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AccountCard } from './AccountCard';
import zhCN from '../../i18n/zh-CN.json';
import type { Account } from '../../store/shared';
import { formatDateTime } from '../../utils/format';

const mocks = vi.hoisted(() => ({
  groups: vi.fn(), send: vi.fn(), toast: vi.fn(), navigate: vi.fn(), copy: vi.fn(),
}));
vi.mock('../../store/groups', () => ({ useGroupsState: mocks.groups }));
vi.mock('../../i18n', () => ({
  useI18n: () => ({ t: (key: string, fallback?: string) => zhCN[key as keyof typeof zhCN] || fallback || key }),
}));
vi.mock('../../api', () => ({ apiSend: mocks.send, isAuthError: () => false }));
vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ showToast: mocks.toast }) }));
vi.mock('react-router-dom', () => ({ useNavigate: () => mocks.navigate }));
vi.mock('../../utils/clipboard', () => ({ copyToClipboard: mocks.copy }));

const baseAccount: Account = {
  biz: 'Mz1', source: 'weixin', nickname: '中投数研', alias: 'research', round_head_img: '',
  avatar_url: '/api/account/Mz1/avatar', group_id: 1, is_disabled: false,
  last_synced_at: null, latest_publish_at: null, sync_interval_days: null,
  article_count: 1187, backfill_pending: false, backfill_running: false,
};
const openDetails = () => {
  const trigger = screen.getByRole('button', { name: /详情/ });
  trigger.focus();
  fireEvent.click(trigger);
  return screen.getByRole('dialog');
};

beforeEach(() => {
  vi.clearAllMocks();
  mocks.groups.mockReturnValue({ state: { selectedAccounts: [] }, dispatch: vi.fn() });
  mocks.send.mockResolvedValue({});
  // jsdom has no native dialog implementation; browser focus trapping is checked separately.
  HTMLDialogElement.prototype.showModal = function () {
    this.open = true;
    this.querySelector<HTMLButtonElement>('button')?.focus();
  };
  HTMLDialogElement.prototype.close = function () { this.open = false; };
});

describe('account cards and details', () => {
  it('shows identity on the card and keeps technical identifiers and sync details in the modal', () => {
    render(<AccountCard account={baseAccount} />);
    expect(screen.getByText('微信公众号 · research')).toBeTruthy();
    expect(screen.queryByText('Mz1')).toBeNull();
    expect(screen.queryByText('1,187')).toBeNull();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.querySelector('details')).toBeNull();

    const dialog = openDetails();
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(within(dialog).getByText('Mz1')).toBeTruthy();
    expect(within(dialog).getByText('1,187')).toBeTruthy();
  });

  it('does not display the opaque biz in place of a missing alias', () => {
    render(<AccountCard account={{ ...baseAccount, alias: null }} />);
    expect(screen.getByText('微信公众号')).toBeTruthy();
    expect(screen.queryByText('Mz1')).toBeNull();
  });

  it('shows publication time on the card and check time only in the modal', () => {
    vi.spyOn(Date, 'now').mockReturnValue(new Date('2026-10-20T08:00:00+08:00').getTime());
    render(<AccountCard account={{ ...baseAccount,
      latest_publish_at: '2026-10-20T01:00:00+08:00', last_synced_at: '2026-10-20T05:49:00+08:00',
    }} />);
    expect(screen.getByText('最新文章 7 小时前')).toBeTruthy();
    expect(screen.queryByText('2 小时前')).toBeNull();
    const dialog = openDetails();
    expect(within(dialog).getByText('2 小时前')).toBeTruthy();
    expect(within(dialog).getByText(formatDateTime('2026-10-20T05:49:00+08:00'))).toBeTruthy();
    expect(within(dialog).getByText(formatDateTime('2026-10-20T01:00:00+08:00'))).toBeTruthy();
  });

  it('does not substitute the check time for an unknown publication time', () => {
    render(<AccountCard account={{ ...baseAccount, last_synced_at: '2026-10-20T05:49:00+08:00' }} />);
    expect(screen.getByText(zhCN['accounts.lastUpdatedEmpty'])).toBeTruthy();
    expect(screen.queryByText(/最新文章/)).toBeNull();
  });

  it.each([
    { running: true, label: '同步中', detail: zhCN['accounts.syncing'] },
    { running: false, label: '等待同步', detail: zhCN['accounts.syncQueued'] },
  ])('distinguishes running and queued history: $running', ({ running, label, detail }) => {
    render(<AccountCard account={{ ...baseAccount, backfill_pending: true, backfill_running: running }} />);
    expect(screen.getByText(label)).toBeTruthy();
    expect(document.querySelector('.sync-dot') !== null).toBe(running);
    expect(within(openDetails()).getByText(detail)).toBeTruthy();
  });

  it('shows pending images as waiting and keeps complete history and counts in the modal', () => {
    render(<AccountCard account={{ ...baseAccount, sync: {
      history: { status: 'complete', task_id: null, error: null }, listing: null, last_checked_at: null,
      articles: { pending: 0, processing: 0, failed: 0 }, images: { pending: 10, failed: 0 },
    } }} />);
    expect(screen.getByText('等待同步')).toBeTruthy();
    expect(screen.queryByText('图片待下载 10 张')).toBeNull();
    const dialog = openDetails();
    expect(within(dialog).getByText('历史列表已补全')).toBeTruthy();
    expect(within(dialog).getByText('图片待下载 10 张')).toBeTruthy();
  });

  it('keeps failures visible when a subscription is paused', () => {
    render(<AccountCard account={{ ...baseAccount, is_disabled: true, sync: {
      history: { status: 'failed', task_id: null, error: 'Request failed' }, listing: null, last_checked_at: null,
      articles: { pending: 0, processing: 0, failed: 0 },
    } }} />);
    expect(screen.getByText('已暂停')).toBeTruthy();
    expect(screen.getByText(zhCN['accounts.syncFailedShort'])).toBeTruthy();
  });

  it('keeps selection and article navigation separate from opening details', () => {
    render(<AccountCard account={baseAccount} />);
    fireEvent.click(screen.getByRole('checkbox'));
    expect(mocks.groups().dispatch).toHaveBeenCalledWith({ type: 'TOGGLE_SELECTED', biz: 'Mz1' });
    fireEvent.click(screen.getByRole('button', { name: '中投数研' }));
    expect(mocks.navigate).toHaveBeenCalledWith('/articles?group=1&account=Mz1');
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it.each(['close', 'cancel', 'backdrop'])('closes via %s and restores focus', (method) => {
    render(<AccountCard account={baseAccount} />);
    const trigger = screen.getByRole('button', { name: /详情/ });
    const dialog = openDetails();
    expect(document.documentElement.style.overflow).toBe('hidden');
    if (method === 'close') fireEvent.click(within(dialog).getByRole('button', { name: '关闭' }));
    else if (method === 'cancel') fireEvent(dialog, new Event('cancel', { bubbles: false }));
    else fireEvent.click(dialog);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(document.documentElement.style.overflow).not.toBe('hidden');
  });

  it('does not close when clicking content and can copy the complete biz', async () => {
    render(<AccountCard account={baseAccount} />);
    const dialog = openDetails();
    fireEvent.click(within(dialog).getByText('Mz1'));
    expect(screen.getByRole('dialog')).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: /复制/ }));
    await waitFor(() => expect(mocks.copy).toHaveBeenCalledWith('Mz1'));
  });

  it('resumes a paused subscription from the dialog', async () => {
    render(<AccountCard account={{ ...baseAccount, is_disabled: true }} />);
    const dialog = openDetails();
    fireEvent.click(within(dialog).getByRole('button', { name: '恢复同步' }));
    await waitFor(() => expect(mocks.send).toHaveBeenCalledWith('/api/account/Mz1', 'PATCH', { is_disabled: false }));
    expect(mocks.groups().dispatch).toHaveBeenCalledWith({ type: 'UPDATE_ACCOUNT', biz: 'Mz1', patch: { is_disabled: false } });
  });

  it('retains the saved interval after an update fails', async () => {
    mocks.send.mockRejectedValueOnce(new Error('offline'));
    render(<AccountCard account={{ ...baseAccount, source: 'youtube', sync_interval_days: 7 }} />);
    const select = within(openDetails()).getByRole('combobox') as HTMLSelectElement;
    fireEvent.change(select, { target: { value: '3' } });
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith(zhCN['accounts.syncFailed']));
    expect(select.value).toBe('7');
    expect(mocks.groups().dispatch).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'UPDATE_ACCOUNT' }));
  });

  it('describes WeChat scheduling without offering an unsupported interval control', () => {
    render(<AccountCard account={baseAccount} />);
    const dialog = openDetails();
    expect(within(dialog).queryByRole('combobox')).toBeNull();
    expect(within(dialog).getByText(/实时推送/)).toBeTruthy();
  });
});
