import { HashRouter, Routes, Route, Navigate } from 'react-router-dom';
import { I18nProvider } from './i18n';
import { ToastProvider } from './hooks/useToast';
import { StoreProvider } from './store';
import { AppShell } from './components/AppShell';
import { AppErrorBoundary } from './components/ErrorBoundary';
import { GroupsPage } from './pages/groups/GroupsPage';
import { ArticlesPage } from './pages/articles/ArticlesPage';
import { SettingsPage } from './pages/settings/SettingsPage';
import { SettingsEmailRoute } from './pages/settings/SettingsEmailRoute';
import { SettingsFilterRoute } from './pages/settings/SettingsFilterRoute';
import { SettingsLoginRoute } from './pages/settings/SettingsLoginRoute';
import { SettingsSyncRoute } from './pages/settings/SettingsSyncRoute';

export default function App() {
  return (
    <I18nProvider>
      <ToastProvider>
        <StoreProvider>
          <AppErrorBoundary>
            <HashRouter>
              <AppShell>
                <Routes>
                  <Route path="/groups" element={<GroupsPage />} />
                  <Route path="/articles" element={<ArticlesPage />} />
                  <Route path="/settings" element={<SettingsPage />}>
                    <Route index element={<Navigate to="sync" replace />} />
                    <Route path="login" element={<SettingsLoginRoute />} />
                    <Route path="sync" element={<SettingsSyncRoute />} />
                    <Route path="filter" element={<SettingsFilterRoute />} />
                    <Route path="email" element={<SettingsEmailRoute />} />
                  </Route>
                  <Route path="*" element={<Navigate to="/groups" replace />} />
                </Routes>
              </AppShell>
            </HashRouter>
          </AppErrorBoundary>
        </StoreProvider>
      </ToastProvider>
    </I18nProvider>
  );
}
