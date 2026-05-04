import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { LoginPanel } from './LoginPanel';

const settingsStateMock = vi.fn();
const apiSendMock = vi.fn();
const emitRefreshMock = vi.fn();
const dispatchMock = vi.fn();

vi.mock('../../store/settings', () => ({
  useSettingsState: () => settingsStateMock(),
}));

vi.mock('../../i18n', () => ({
  useI18n: () => ({
    t: (key: string, fallback?: string) => {
      const dict: Record<string, string> = {
        'login.title': '登录',
        'login.start': '开始登录',
        'login.refresh': '刷新登录',
        'login.cancel': '取消',
        'login.status.waiting': '请使用微信扫码',
      };
      return dict[key] || fallback || key;
    },
  }),
}));

vi.mock('../../api', () => ({
  apiSend: (...args: unknown[]) => apiSendMock(...args),
  isAuthError: () => false,
}));

vi.mock('../../utils/events', () => ({
  emitRefresh: () => emitRefreshMock(),
}));

describe('LoginPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    settingsStateMock.mockReturnValue({
      state: {
        loginStatus: {
          status: 'idle',
          message: '',
          qrcode_url: null,
          last_login: null,
        },
      },
      dispatch: dispatchMock,
    });
  });

  it('emits a refresh after starting login so polling can begin', async () => {
    apiSendMock.mockResolvedValue({
      status: 'waiting',
      message: 'Scan the QR code with WeChat',
      qrcode_url: '/api/login/qrcode',
      last_login: null,
    });

    render(<LoginPanel />);

    await fireEvent.click(screen.getByRole('button', { name: '开始登录' }));

    expect(apiSendMock).toHaveBeenCalledWith('/api/login/start', 'POST', { force: false });
    expect(dispatchMock).toHaveBeenCalledWith({
      type: 'SET_LOGIN_STATUS',
      payload: {
        status: 'waiting',
        message: 'Scan the QR code with WeChat',
        qrcode_url: '/api/login/qrcode',
        last_login: null,
      },
    });
    expect(emitRefreshMock).toHaveBeenCalledTimes(1);
  });
});
