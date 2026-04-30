import { HashRouter, Routes, Route, Navigate } from 'react-router-dom';
import { I18nProvider } from './i18n';
import { ToastProvider } from './hooks/useToast';
import { StoreProvider } from './store';
import { AppShell } from './components/AppShell';
import { GroupsPage } from './pages/groups/GroupsPage';
import { ArticlesPage } from './pages/articles/ArticlesPage';
import { SettingsPage } from './pages/settings/SettingsPage';

export default function App() {
  return (
    <I18nProvider>
      <ToastProvider>
        <StoreProvider>
          <HashRouter>
            <AppShell>
              <Routes>
                <Route path="/groups" element={<GroupsPage />} />
                <Route path="/articles" element={<ArticlesPage />} />
                <Route path="/settings" element={<SettingsPage />} />
                <Route path="*" element={<Navigate to="/groups" replace />} />
              </Routes>
            </AppShell>
          </HashRouter>
        </StoreProvider>
      </ToastProvider>
    </I18nProvider>
  );
}
