import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AppShell } from './AppShell';
import { I18nProvider } from '../i18n';
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

/** Renders the shell with the signed-in user the frame depends on. */
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
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('renders the frame without probing daemon or sync infrastructure', () => {
    vi.stubGlobal('ResizeObserver', ResizeObserverMock);

    renderShell();

    expect(screen.getByText('content')).toBeTruthy();
    expect(apiGetMock).not.toHaveBeenCalled();
    expect(document.querySelector('#daemon-status')).toBeNull();
    expect(document.querySelector('#status-banner')).toBeNull();
  });

  it('keeps the shell the same for an ordinary reader', () => {
    vi.stubGlobal('ResizeObserver', ResizeObserverMock);

    renderShell({ ...adminUser, role: 'user' });

    expect(apiGetMock).not.toHaveBeenCalled();
    expect(document.querySelector('.banner')).toBeNull();
  });
});
