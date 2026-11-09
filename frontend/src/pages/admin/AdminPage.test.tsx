import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import { AdminPage } from './AdminPage';

const mocks = vi.hoisted(() => ({ get: vi.fn(), send: vi.fn(), toast: vi.fn() }));
vi.mock('../../api', () => ({ apiGet: mocks.get, apiSend: mocks.send }));
vi.mock('../../hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 1 } }) }));
vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ showToast: mocks.toast }) }));

const users = [
  { id: 1, username: 'root', email: null, role: 'admin', is_disabled: false, session_count: 1, last_login_at: null },
  { id: 7, username: 'alice', email: null, role: 'user', is_disabled: false, session_count: 0, last_login_at: null },
];
const renderPage = () => render(<I18nProvider><AdminPage /></I18nProvider>);
const targetRow = () => within(screen.getByRole('row', { name: /alice/ }));

describe('user administration', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.get.mockResolvedValue({ items: users });
    mocks.send.mockResolvedValue({});
  });

  it('confirms deletion, describes its scope, and refreshes the user list', async () => {
    renderPage();
    await screen.findByText('alice');
    fireEvent.click(targetRow().getByRole('button', { name: '删除' }));
    const dialog = within(screen.getByRole('dialog', { name: '删除用户' }));
    expect(dialog.getByText(/用户 alice.*无法恢复.*共享文章和图片会保留/)).toBeTruthy();
    expect(mocks.send).not.toHaveBeenCalled();
    mocks.get.mockResolvedValue({ items: [users[0]] });
    await act(async () => fireEvent.click(dialog.getByRole('button', { name: '删除' })));
    expect(mocks.send).toHaveBeenCalledWith('/api/admin/user/7', 'DELETE', {});
    expect(screen.queryByText('alice')).toBeNull();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('cancels deletion without changing the account', async () => {
    renderPage();
    await screen.findByText('alice');
    fireEvent.click(targetRow().getByRole('button', { name: '删除' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '取消' }));
    expect(mocks.send).not.toHaveBeenCalled();
    expect(screen.getByText('alice')).toBeTruthy();
  });

  it('retains the account and confirmation when deletion fails', async () => {
    mocks.send.mockRejectedValueOnce(new Error('Deletion refused'));
    renderPage();
    await screen.findByText('alice');
    fireEvent.click(targetRow().getByRole('button', { name: '删除' }));
    await act(async () => fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '删除' })));
    expect(screen.getByRole('alert').textContent).toBe('Deletion refused');
    expect(screen.getByText('alice')).toBeTruthy();
    expect(mocks.get).toHaveBeenCalledTimes(1);
  });

  it('disables operations that would remove the acting admin access', async () => {
    renderPage();
    await screen.findByText('alice');
    const row = within(screen.getByRole('row', { name: /root/ }));
    for (const name of ['禁用', '删除']) {
      expect((row.getByRole('button', { name }) as HTMLButtonElement).disabled).toBe(true);
    }
    expect((row.getByRole('combobox', { name: 'root 的角色' }) as HTMLSelectElement).disabled).toBe(true);
  });

  it('shows a failed load with retry instead of an empty user count', async () => {
    mocks.get.mockRejectedValueOnce(new Error('Users unavailable'));
    renderPage();
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.queryByText('0 个账号')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(await screen.findByText('alice')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('prevents duplicate user creation while the request is pending', async () => {
    let finish!: () => void;
    mocks.send.mockImplementationOnce(() => new Promise<void>((resolve) => { finish = resolve; }));
    renderPage();
    await screen.findByText('alice');
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'bob' } });
    fireEvent.change(screen.getByLabelText('初始密码'), { target: { value: 'a-good-password' } });
    const button = screen.getByRole('button', { name: '创建' });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(mocks.send).toHaveBeenCalledTimes(1);
    expect((button as HTMLButtonElement).disabled).toBe(true);
    await act(async () => finish());
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  });
});
