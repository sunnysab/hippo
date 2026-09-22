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

vi.mock('../../utils/sync', () => ({
  getSyncTone: () => 'neutral',
}));

const loggedOut = {
  logged_in: false,
  status: 'logged_out',
  need_relogin: false,
  wxid: null,
  nickname: null,
  head_url: null,
  clients_connected: null,
  error: null,
};

const loggedIn = {
  logged_in: true,
  status: 'online',
  need_relogin: false,
  wxid: 'wxid_tester',
  nickname: 'tester',
  head_url: null,
  clients_connected: 2,
  error: null,
};

describe('LoginPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    settingsStateMock.mockReturnValue({
      state: { loginStatus: loggedOut },
      dispatch: dispatchMock,
    });
    apiGetMock.mockResolvedValue(loggedIn);
  });

  it('renders the daemon login status', () => {
    render(<LoginPanel />);
    expect(screen.getByText('daemon is not signed in')).toBeTruthy();
    expect(screen.getByText('daemon is not signed in; scan or use auto re-login.')).toBeTruthy();
  });

  it('renders nickname and client count when signed in', () => {
    settingsStateMock.mockReturnValue({
      state: { loginStatus: loggedIn },
      dispatch: dispatchMock,
    });
    render(<LoginPanel />);
    expect(screen.getByText('tester · 2 clients connected')).toBeTruthy();
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

    fireEvent.click(screen.getByRole('button', { name: 'Auto re-login' }));

    await screen.findByRole('button', { name: 'Auto re-login' });
    expect(apiSendMock).toHaveBeenCalledWith('/api/login/auto', 'POST', {});
    expect(apiGetMock).toHaveBeenCalledWith('/api/login');
    expect(dispatchMock).toHaveBeenCalledWith({
      type: 'SET_LOGIN_STATUS',
      payload: loggedIn,
    });
    expect(emitRefreshMock).toHaveBeenCalled();
  });
});
