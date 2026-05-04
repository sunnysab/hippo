import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SyncSettingsPanel } from './SyncSettingsPanel';

const settingsStateMock = vi.fn();
const showToastMock = vi.fn();
const apiSendMock = vi.fn();
const apiGetMock = vi.fn();
const emitRefreshMock = vi.fn();

vi.mock('../../store/settings', () => ({
  useSettingsState: () => settingsStateMock(),
}));

vi.mock('../../i18n', () => ({
  useI18n: () => ({
    t: (key: string, fallback?: string) => {
      const dict: Record<string, string> = {
        'sync.run': '运行',
        'sync.runningProgress': '处理中',
        'sync.runQueued': '同步任务已进入队列',
        'sync.loginRequired': '登录失效，请重新登录。',
      };
      return dict[key] || fallback || key;
    },
  }),
}));

vi.mock('../../hooks/useToast', () => ({
  useToast: () => ({ showToast: showToastMock }),
}));

vi.mock('../../api', () => ({
  apiSend: (...args: unknown[]) => apiSendMock(...args),
  apiGet: (...args: unknown[]) => apiGetMock(...args),
  isAuthError: () => false,
}));

vi.mock('../../utils/events', () => ({
  emitRefresh: () => emitRefreshMock(),
}));

describe('SyncSettingsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    settingsStateMock.mockReturnValue({
      state: {
        syncSettings: {
          window_start_hour: 6,
          window_end_hour: 24,
        },
      },
      dispatch: vi.fn(),
    });
  });

  it('polls the queued task and surfaces login-required failures immediately', async () => {
    apiSendMock.mockResolvedValue({
      status: 'queued',
      task_id: 'task-1',
    });
    apiGetMock
      .mockResolvedValueOnce({
        task_id: 'task-1',
        status: 'running',
      })
      .mockResolvedValueOnce({
        task_id: 'task-1',
        status: 'login_required',
        error: 'invalid session',
      });

    render(
      <SyncSettingsPanel
        formState={{
          enabled: true,
          intervalMinutes: '60',
          windowStartHour: '6',
          windowEndHour: '24',
          sleepSeconds: '0.05',
          skipMinutes: '30',
          downloadContent: true,
          downloadImages: true,
          articleExcludeKeywords: '',
          alertEnabled: false,
          alertEmail: '',
          smtpHost: '',
          smtpPort: '587',
          smtpUser: '',
          smtpPassword: '',
          smtpTls: true,
          fromEmail: '',
        }}
        setFormState={vi.fn()}
      />,
    );

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '运行' }));
    });

    expect(showToastMock).toHaveBeenCalledWith('同步任务已进入队列');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });

    expect(apiGetMock).toHaveBeenCalledWith('/api/settings/tasks/task-1');
    expect(showToastMock).toHaveBeenLastCalledWith('登录失效，请重新登录。');
    expect(emitRefreshMock).toHaveBeenCalledTimes(1);
  });
});
