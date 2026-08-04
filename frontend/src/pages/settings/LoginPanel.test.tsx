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
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

vi.mock('../../api', () => ({
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

const missingStatus = {
  status: 'missing',
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
      state: { loginStatus: missingStatus },
      dispatch: dispatchMock,
    });
  });

  it('imports pasted credentials via /api/login/import', async () => {
    apiSendMock.mockResolvedValue({ status: 'ok', has_credential: true, vid: '200' });

    render(<LoginPanel />);

    fireEvent.change(screen.getByLabelText('vid'), { target: { value: '200' } });
    fireEvent.change(screen.getByLabelText('accessToken'), { target: { value: 'tok' } });
    fireEvent.change(screen.getByLabelText('refreshToken'), { target: { value: 'rt' } });
    fireEvent.change(screen.getByLabelText('deviceId'), { target: { value: 'dev' } });

    await fireEvent.click(screen.getByRole('button', { name: 'Import' }));

    expect(apiSendMock).toHaveBeenCalledWith('/api/login/import', 'POST', {
      vid: '200',
      access_token: 'tok',
      refresh_token: 'rt',
      device_id: 'dev',
    });
    expect(dispatchMock).toHaveBeenCalledWith({
      type: 'SET_LOGIN_STATUS',
      payload: { status: 'ok', has_credential: true, vid: '200' },
    });
    expect(emitRefreshMock).toHaveBeenCalledTimes(1);
  });

  it('refreshes the token when a credential is present', async () => {
    apiSendMock.mockResolvedValue({ status: 'ok', has_credential: true, vid: '200' });
    settingsStateMock.mockReturnValue({
      state: {
        loginStatus: {
          status: 'ok',
          message: '',
          has_credential: true,
          vid: '200',
          nickname: 'acc',
          avatar: null,
          updated_at: '2026-05-04T00:00:00.000Z',
          last_error: null,
        },
      },
      dispatch: dispatchMock,
    });

    render(<LoginPanel />);

    await fireEvent.click(screen.getByRole('button', { name: 'Refresh Token' }));

    expect(apiSendMock).toHaveBeenCalledWith('/api/login/refresh', 'POST', {});
  });
});
