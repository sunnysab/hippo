import { act, fireEvent, render, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import { SettingsProvider } from '../../store/settings';
import { emitRefresh } from '../../utils/events';
import { SettingsEmailRoute } from './SettingsEmailRoute';
import { SettingsFilterRoute } from './SettingsFilterRoute';
import { SettingsPage } from './SettingsPage';

const hoisted = vi.hoisted(() => ({
  isAdmin: false,
  apiGet: vi.fn(),
  apiSend: vi.fn(),
}));

vi.mock('../../api', () => ({
  apiGet: (path: string) => hoisted.apiGet(path),
  apiSend: (...args: unknown[]) => hoisted.apiSend(...args),
  isAuthError: () => false,
}));

vi.mock('../../hooks/useAuth', () => ({
  useAuth: () => ({ isAdmin: hoisted.isAdmin }),
}));

vi.mock('../../hooks/useToast', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

vi.mock('../../hooks/useReaderSettings', () => ({
  useReaderSettings: () => ({ config: { hideSmall: false }, updateConfig: vi.fn() }),
}));

const PREFERENCES = { timezone: 'Asia/Shanghai', article_exclude_keywords: 'seed' };

const settingsPayload = (overrides: Record<string, unknown> = {}): Record<string, unknown> => ({
  enabled: true,
  interval_minutes: 60,
  sleep_seconds: 15,
  skip_minutes: 30,
  download_content: true,
  download_images: true,
  article_exclude_keywords: '',
  alert_enabled: false,
  alert_email: '',
  email: {
    smtp_host: 'smtp.example.com',
    smtp_port: 587,
    smtp_user: 'user',
    smtp_password: '••••••',
    smtp_password_set: true,
    smtp_tls: true,
    from_email: 'no-reply@example.com',
  },
  ...overrides,
});

const renderSettings = (path: string) => render(
  <MemoryRouter initialEntries={[path]}>
    <I18nProvider>
      <SettingsProvider>
        <Routes>
          <Route path="/settings" element={<SettingsPage />}>
            <Route path="filter" element={<SettingsFilterRoute />} />
            <Route path="email" element={<SettingsEmailRoute />} />
          </Route>
        </Routes>
      </SettingsProvider>
    </I18nProvider>
  </MemoryRouter>,
);

const filterField = () => document.querySelector('#sync-article-exclude-keywords') as HTMLTextAreaElement;
const smtpHostField = () => document.querySelector('#email-smtp-host') as HTMLInputElement;
const settingsCalls = () => hoisted.apiGet.mock.calls.filter(([path]) => path === '/api/settings').length;

describe('SettingsPage', () => {
  let settingsOverrides: Record<string, unknown>;

  beforeEach(() => {
    hoisted.isAdmin = false;
    hoisted.apiGet.mockReset();
    hoisted.apiSend.mockReset();
    settingsOverrides = {};
    hoisted.apiGet.mockImplementation((path: string) => {
      if (path === '/api/settings/preferences') return Promise.resolve(PREFERENCES);
      if (path === '/api/settings') return Promise.resolve(settingsPayload(settingsOverrides));
      return Promise.resolve({});
    });
  });

  it("loads a normal user's own reading preferences without any admin request", async () => {
    renderSettings('/settings/filter');
    await waitFor(() => expect(filterField().value).toBe('seed'));

    const paths = hoisted.apiGet.mock.calls.map(([path]) => path as string);
    expect(paths).not.toContain('/api/settings');
    expect(hoisted.apiSend).not.toHaveBeenCalled();
  });

  it('keeps an unsaved personal filter draft when the instance settings change', async () => {
    hoisted.isAdmin = true;
    renderSettings('/settings/filter');
    await waitFor(() => expect(filterField().value).toBe('seed'));
    await waitFor(() => expect(settingsCalls()).toBe(1));

    fireEvent.change(filterField(), { target: { value: 'draft' } });

    settingsOverrides = { interval_minutes: 120 };
    act(() => { emitRefresh(); });
    await waitFor(() => expect(settingsCalls()).toBe(2));

    // The global settings re-seeded the sync/email form, not the filter draft.
    await waitFor(() => expect(filterField().value).toBe('draft'));
  });

  it('does not reset an email field when a refresh returns the same settings', async () => {
    hoisted.isAdmin = true;
    renderSettings('/settings/email');
    await waitFor(() => expect(smtpHostField().value).toBe('smtp.example.com'));
    await waitFor(() => expect(settingsCalls()).toBe(1));

    fireEvent.change(smtpHostField(), { target: { value: 'typed.example.com' } });

    act(() => { emitRefresh(); });
    await waitFor(() => expect(settingsCalls()).toBe(2));

    await waitFor(() => expect(smtpHostField().value).toBe('typed.example.com'));
  });
});
