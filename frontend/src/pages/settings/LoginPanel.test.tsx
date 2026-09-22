import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { LoginPanel } from './LoginPanel';

const settingsStateMock = vi.fn();
const apiGetMock = vi.fn();
const apiSendMock = vi.fn();
const emitRefreshMock = vi.fn();
const dispatchMock = vi.fn();

vi.mock('../../store/settings', () => ({
  useSettingsState: () => settingsStateMock(),
}));

vi.mock('../../i18n', () => ({
  useI18n: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

vi.mock('../../api', () => ({
  apiGet: (...args: unknown[]) => apiGetMock(...args),
  apiSend: (...args: unknown[]) => apiSendMock(...args),
  isAuthError: () => false,
}));

vi.mock('../../utils/events', () => ({
  emitRefresh: () => emitRefreshMock(),
}));

vi.mock('../../utils/format', () => ({
  formatRelativeTime: () => 'recently',
}));

vi.mock('../../utils/sync', () => ({
  getSyncTone: () => 'neutral',
}));

const loggedOut = {
  status: 'logged_out',
  message: '',
  has_credential: false,
  vid: null,
  nickname: null,
  avatar: null,
  updated_at: null,
  last_error: null,
};

describe('LoginPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    settingsStateMock.mockReturnValue({
      state: { loginStatus: loggedOut },
      dispatch: dispatchMock,
    });
    apiGetMock.mockResolvedValue({ status: 'online', has_credential: true, nickname: 'tester' });
  });

  it('renders the daemon login status', () => {
    render(<LoginPanel />);
    expect(screen.getByText('daemon is not signed in.')).toBeTruthy();
  });

  it('fetches a QR code via /api/login/qr and renders it', async () => {
    apiSendMock.mockResolvedValue({ png_base64: 'AAAA', url: 'https://weixin.qq.com/x' });
    render(<LoginPanel />);

    fireEvent.click(screen.getByRole('button', { name: 'Scan QR' }));

    await screen.findByAltText('login qr');
    expect(apiSendMock).toHaveBeenCalledWith('/api/login/qr', 'POST', {});
  });

  it('re-logs in via /api/login/auto and refreshes status', async () => {
    apiSendMock.mockResolvedValue({ ok: true });
    render(<LoginPanel />);

    fireEvent.click(screen.getByRole('button', { name: 'Re-login' }));

    await screen.findByRole('button', { name: 'Re-login' });
    expect(apiSendMock).toHaveBeenCalledWith('/api/login/auto', 'POST', {});
    expect(apiGetMock).toHaveBeenCalledWith('/api/login');
    expect(dispatchMock).toHaveBeenCalledWith({
      type: 'SET_LOGIN_STATUS',
      payload: { status: 'online', has_credential: true, nickname: 'tester' },
    });
    expect(emitRefreshMock).toHaveBeenCalled();
  });
});
