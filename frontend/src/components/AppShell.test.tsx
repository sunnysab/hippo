import { act, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { AppShell } from './AppShell';
import { I18nProvider } from '../i18n';
import { ToastProvider } from '../hooks/useToast';

const apiGetMock = vi.fn();

vi.mock('../api', () => ({
  apiGet: (path: string) => apiGetMock(path),
}));

class ResizeObserverMock {
  observe() {}
  disconnect() {}
}

describe('AppShell', () => {
  it('refreshes the daemon status and last sync info every minute', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-02T12:00:00.000Z'));
    vi.stubGlobal('ResizeObserver', ResizeObserverMock);

    apiGetMock.mockImplementation(async (path: string) => {
      if (path === '/api/login') {
        return {
          logged_in: true,
          status: 'online',
          nickname: 'tester',
        };
      }

      if (path === '/api/settings/status') {
        return {
          last_finished_at: '2026-05-02T11:58:00.000Z',
        };
      }

      throw new Error(`unexpected path: ${path}`);
    });

    render(
      <MemoryRouter>
        <I18nProvider>
          <ToastProvider>
            <AppShell>
              <div>content</div>
            </AppShell>
          </ToastProvider>
        </I18nProvider>
      </MemoryRouter>,
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(screen.getByText('daemon 在线')).toBeTruthy();
    expect(screen.getByText('上次同步于 2 分钟前')).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60000);
    });

    expect(screen.getByText('上次同步于 3 分钟前')).toBeTruthy();
    expect(apiGetMock).toHaveBeenCalledTimes(4);

    vi.useRealTimers();
  });

  it('shows the login banner when the daemon is not signed in', async () => {
    vi.stubGlobal('ResizeObserver', ResizeObserverMock);

    apiGetMock.mockImplementation(async (path: string) => {
      if (path === '/api/login') {
        return { logged_in: false, status: 'logged_out', error: '' };
      }
      if (path === '/api/settings/status') {
        return { last_finished_at: null, last_error: null };
      }
      throw new Error(`unexpected path: ${path}`);
    });

    render(
      <MemoryRouter>
        <I18nProvider>
          <ToastProvider>
            <AppShell>
              <div>content</div>
            </AppShell>
          </ToastProvider>
        </I18nProvider>
      </MemoryRouter>,
    );

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.getByText('daemon 未登录')).toBeTruthy();
    expect(screen.getByText('登录失效，请重新登录。')).toBeTruthy();
  });
});
