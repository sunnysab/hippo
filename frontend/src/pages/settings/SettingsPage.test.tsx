import { render, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import { SettingsPage } from './SettingsPage';
import { SettingsFilterRoute } from './SettingsFilterRoute';

const apiGetMock = vi.fn();
const apiSendMock = vi.fn();

vi.mock('../../api', () => ({
  apiGet: (...args: unknown[]) => apiGetMock(...args),
  apiSend: (...args: unknown[]) => apiSendMock(...args),
  isAuthError: () => false,
}));

vi.mock('../../hooks/useAuth', () => ({
  useAuth: () => ({ isAdmin: false, user: { id: 1, username: 'reader', role: 'user' } }),
}));

vi.mock('../../store/settings', () => ({
  useSettingsState: () => ({ state: { syncSettings: null }, dispatch: vi.fn() }),
}));

vi.mock('../../hooks/useToast', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

vi.mock('../../hooks/useReaderSettings', () => ({
  useReaderSettings: () => ({ config: { hideSmall: false }, updateConfig: vi.fn() }),
}));

describe('SettingsPage', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiSendMock.mockReset();
    apiGetMock.mockResolvedValue({ timezone: 'Asia/Shanghai', article_exclude_keywords: '' });
  });

  it('loads a normal user\'s own reading preferences without any admin request', async () => {
    render(
      <MemoryRouter initialEntries={['/settings/filter']}>
        <I18nProvider>
          <Routes>
            <Route path="/settings" element={<SettingsPage />}>
              <Route path="filter" element={<SettingsFilterRoute />} />
            </Route>
          </Routes>
        </I18nProvider>
      </MemoryRouter>,
    );

    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/settings/preferences'));
    const paths = apiGetMock.mock.calls.map(([path]) => path as string);
    expect(paths).not.toContain('/api/settings');
    expect(apiSendMock).not.toHaveBeenCalled();
  });
});
