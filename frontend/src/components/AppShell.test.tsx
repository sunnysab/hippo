import { act, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AppShell } from './AppShell';
import { I18nProvider } from '../i18n';
import zhCN from '../i18n/zh-CN.json';
import { ToastProvider } from '../hooks/useToast';

const apiGetMock = vi.fn();
const authMock = vi.fn();

vi.mock('../api', () => ({
  apiGet: (path: string) => apiGetMock(path),
}));

vi.mock('../hooks/useAuth', () => ({
  useAuth: () => authMock(),
}));

const adminUser = {
  id: 1,
  username: 'admin',
  email: null,
  email_verified: true,
  role: 'admin',
  timezone: 'UTC',
};

function LocationProbe() {
  const { pathname } = useLocation();
  return <div data-testid="location">{pathname}</div>;
}

/** Renders the shell with the signed-in user the banner conditions depend on. */
function renderShell(user: typeof adminUser = adminUser) {
  authMock.mockReturnValue({
    status: 'authenticated',
    user,
    isAdmin: user.role === 'admin',
    refresh: vi.fn(),
    signIn: vi.fn(),
    signOut: vi.fn(),
  });
  return render(
    <MemoryRouter>
      <LocationProbe />
      <I18nProvider>
        <ToastProvider>
          <AppShell>
            <div>content</div>
          </AppShell>
        </ToastProvider>
      </I18nProvider>
    </MemoryRouter>,
  );
}

class ResizeObserverMock {
  observe() {}
  disconnect() {}
}

describe('AppShell', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });
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

    renderShell();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(screen.getByText('同步服务运行中')).toBeTruthy();
    expect(screen.getByText('上次同步于 2 分钟前')).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60000);
    });

    expect(screen.getByText('上次同步于 3 分钟前')).toBeTruthy();
    expect(apiGetMock).toHaveBeenCalledTimes(4);

    vi.useRealTimers();
  });

  it('reports the newest ingest time, not only the last list sync', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-02T12:00:00.000Z'));
    vi.stubGlobal('ResizeObserver', ResizeObserverMock);

    // The push channel keeps storing articles while the list job is long idle.
    apiGetMock.mockImplementation(async (path: string) => {
      if (path === '/api/login') {
        return { logged_in: true, status: 'online', nickname: 'tester' };
      }
      if (path === '/api/settings/status') {
        return {
          last_finished_at: '2026-04-23T12:00:00.000Z',
          last_ingest_at: '2026-05-02T11:58:00.000Z',
        };
      }
      throw new Error(`unexpected path: ${path}`);
    });

    renderShell();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(screen.getByText('上次同步于 2 分钟前')).toBeTruthy();

    vi.useRealTimers();
  });

  it('shows the login banner when the daemon is not signed in', async () => {
    vi.stubGlobal('ResizeObserver', ResizeObserverMock);

    apiGetMock.mockImplementation(async (path: string) => {
      if (path === '/api/login') {
        return { logged_in: false, status: 'logged_out', need_relogin: false, error: '' };
      }
      if (path === '/api/settings/status') {
        return { last_finished_at: null, last_error: null };
      }
      throw new Error(`unexpected path: ${path}`);
    });

    renderShell();

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.getByText(zhCN['sync.status.offline'])).toBeTruthy();
    expect(screen.getByText(zhCN['banner.weixinNotLoggedIn'])).toBeTruthy();
    // A signed-out daemon can be renewed from the QR page, so the admin keeps it.
    expect(screen.getByRole('button', { name: zhCN['login.relogin'] })).toBeTruthy();
  });

  it('keeps the WeChat wording apart from an ordinary sync failure', async () => {
    vi.stubGlobal('ResizeObserver', ResizeObserverMock);

    apiGetMock.mockImplementation(async (path: string) => {
      if (path === '/api/login') {
        return { logged_in: true, status: 'online', nickname: 'tester' };
      }
      if (path === '/api/settings/status') {
        return { last_finished_at: null, last_error: 'HTTP 502 from account Mz123' };
      }
      throw new Error(`unexpected path: ${path}`);
    });

    renderShell();

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.getByText(zhCN['banner.syncFailed'].replace('{message}', 'HTTP 502 from account Mz123'))).toBeTruthy();
    // A sync job failure is not a WeChat session problem, so no relogin hint.
    expect(screen.queryByText(zhCN['banner.weixinLoginExpired'])).toBeNull();
    expect(screen.queryByRole('button', { name: zhCN['login.relogin'] })).toBeNull();
  });

  it('offers a relogin to admins only when the WeChat session expired', async () => {
    vi.stubGlobal('ResizeObserver', ResizeObserverMock);

    apiGetMock.mockImplementation(async (path: string) => {
      if (path === '/api/login') {
        return { logged_in: false, status: 'expired', need_relogin: true, error: '' };
      }
      if (path === '/api/settings/status') {
        return { last_finished_at: null, last_error: null };
      }
      throw new Error(`unexpected path: ${path}`);
    });

    renderShell();

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.getByText(zhCN['banner.weixinLoginExpired'])).toBeTruthy();
    const relogin = screen.getByRole('button', { name: zhCN['login.relogin'] });
    fireEvent.click(relogin);
    expect(screen.getByTestId('location').textContent).toBe('/admin/login');
  });

  it('keeps the expired banner and relogin when a stale sync error is also stored', async () => {
    vi.stubGlobal('ResizeObserver', ResizeObserverMock);

    apiGetMock.mockImplementation(async (path: string) => {
      if (path === '/api/login') {
        return { logged_in: false, status: 'expired', need_relogin: true, error: '' };
      }
      if (path === '/api/settings/status') {
        return { last_finished_at: null, last_error: 'old failure' };
      }
      throw new Error(`unexpected path: ${path}`);
    });

    renderShell();

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.getByText(zhCN['banner.weixinLoginExpired'])).toBeTruthy();
    expect(document.querySelector('.banner.is-hidden')).toBeNull();
    expect(screen.getByRole('button', { name: zhCN['login.relogin'] })).toBeTruthy();
  });

  it('keeps the unreachable banner when a stale sync error is also stored', async () => {
    vi.stubGlobal('ResizeObserver', ResizeObserverMock);

    apiGetMock.mockImplementation(async (path: string) => {
      if (path === '/api/login') {
        return { logged_in: false, status: 'unreachable', need_relogin: false, error: 'connect ECONNREFUSED' };
      }
      if (path === '/api/settings/status') {
        return { last_finished_at: null, last_error: 'old failure' };
      }
      throw new Error(`unexpected path: ${path}`);
    });

    renderShell();

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.getByText(zhCN['banner.weixinUnreachable'] + ' (connect ECONNREFUSED)')).toBeTruthy();
    expect(screen.getByText(zhCN['sync.status.unreachable'])).toBeTruthy();
    expect(screen.queryByRole('button', { name: zhCN['login.relogin'] })).toBeNull();
  });

  it('keeps the login banner when the sync status call fails', async () => {
    vi.stubGlobal('ResizeObserver', ResizeObserverMock);

    apiGetMock.mockImplementation(async (path: string) => {
      if (path === '/api/login') {
        return { logged_in: false, status: 'expired', need_relogin: true, error: '' };
      }
      throw new Error(`unexpected path: ${path}`);
    });

    renderShell();

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.getByText(zhCN['banner.weixinLoginExpired'])).toBeTruthy();
    expect(document.querySelector('.banner.is-hidden')).toBeNull();
    expect(screen.getByRole('button', { name: zhCN['login.relogin'] })).toBeTruthy();
  });

  it('withdraws the expired banner once the daemon signs back in, even if the sync status call fails', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('ResizeObserver', ResizeObserverMock);

    let loggedIn = false;
    apiGetMock.mockImplementation(async (path: string) => {
      if (path === '/api/login') {
        return loggedIn
          ? { logged_in: true, status: 'online', nickname: 'tester' }
          : { logged_in: false, status: 'expired', need_relogin: true, error: '' };
      }
      // The sync status stays broken across both rounds.
      throw new Error(`unexpected path: ${path}`);
    });

    renderShell();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(screen.getByText(zhCN['banner.weixinLoginExpired'])).toBeTruthy();
    expect(screen.getByRole('button', { name: zhCN['login.relogin'] })).toBeTruthy();

    loggedIn = true;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60000);
    });

    expect(screen.queryByText(zhCN['banner.weixinLoginExpired'])).toBeNull();
    expect(screen.queryByRole('button', { name: zhCN['login.relogin'] })).toBeNull();
    expect(document.querySelector('.banner.is-hidden')).not.toBeNull();
    expect(screen.getByText(zhCN['sync.status.online'])).toBeTruthy();
  });

  it('names an unreachable WeChat service without offering a relogin', async () => {
    vi.stubGlobal('ResizeObserver', ResizeObserverMock);

    apiGetMock.mockImplementation(async (path: string) => {
      if (path === '/api/login') {
        return { logged_in: false, status: 'unreachable', need_relogin: false, error: 'connect ECONNREFUSED' };
      }
      if (path === '/api/settings/status') {
        return { last_finished_at: null, last_error: null };
      }
      throw new Error(`unexpected path: ${path}`);
    });

    renderShell();

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.getByText(zhCN['banner.weixinUnreachable'] + ' (connect ECONNREFUSED)')).toBeTruthy();
    expect(screen.queryByRole('button', { name: zhCN['login.relogin'] })).toBeNull();
  });

  it('leaves the dismiss button reachable for non-admins', async () => {
    vi.stubGlobal('ResizeObserver', ResizeObserverMock);

    apiGetMock.mockImplementation(async (path: string) => {
      if (path === '/api/login') {
        return { logged_in: false, status: 'expired', need_relogin: true, error: '' };
      }
      if (path === '/api/settings/status') {
        return { last_finished_at: null, last_error: null };
      }
      throw new Error(`unexpected path: ${path}`);
    });

    renderShell({ ...adminUser, role: 'user' });

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.getByText(zhCN['banner.weixinLoginExpired'])).toBeTruthy();
    expect(screen.queryByRole('button', { name: zhCN['login.relogin'] })).toBeNull();
    expect(screen.getByRole('button', { name: zhCN['common.close'] })).toBeTruthy();
  });
});
