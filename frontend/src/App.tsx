import type { ReactNode } from 'react';
import { HashRouter, Routes, Route, Navigate } from 'react-router-dom';
import { I18nProvider } from './i18n';
import { ToastProvider } from './hooks/useToast';
import { AuthProvider, useAuth } from './hooks/useAuth';
import { StoreProvider } from './store';
import { AppShell } from './components/AppShell';
import { AppErrorBoundary } from './components/ErrorBoundary';
import { LoginPage } from './pages/auth/LoginPage';
import { RegisterPage } from './pages/auth/RegisterPage';
import { VerifyPage } from './pages/auth/VerifyPage';
import { ResetPasswordPage } from './pages/auth/ResetPasswordPage';
import { ForgotPasswordPage } from './pages/auth/ForgotPasswordPage';
import { GroupsPage } from './pages/groups/GroupsPage';
import { ArticlesPage } from './pages/articles/ArticlesPage';
import { SettingsPage } from './pages/settings/SettingsPage';
import { SettingsEmailRoute } from './pages/settings/SettingsEmailRoute';
import { SettingsFilterRoute } from './pages/settings/SettingsFilterRoute';
import { SettingsLoginRoute } from './pages/settings/SettingsLoginRoute';
import { SettingsSyncRoute } from './pages/settings/SettingsSyncRoute';

function RequireAuth({ children }: { children: ReactNode }) {
  const { status } = useAuth();
  if (status === 'loading') {
    return (
      <div className="auth-screen">
        <div className="auth-card auth-card-loading">…</div>
      </div>
    );
  }
  if (status === 'anonymous') {
    return <Navigate to="/login" replace />;
  }
  return <>{children}</>;
}

function AppRoutes() {
  const { status } = useAuth();

  return (
    <Routes>
      <Route
        path="/login"
        element={status === 'authenticated' ? <Navigate to="/groups" replace /> : <LoginPage />}
      />
      {/* Public: reached from e-mail links, before a session exists. */}
      <Route path="/register" element={<RegisterPage />} />
      <Route path="/verify" element={<VerifyPage />} />
      <Route path="/reset" element={<ResetPasswordPage />} />
      <Route path="/forgot" element={<ForgotPasswordPage />} />
      <Route
        path="*"
        element={
          <RequireAuth>
            {/* Mounted only once signed in, so nothing fetches business data anonymously. */}
            <StoreProvider>
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
            </StoreProvider>
          </RequireAuth>
        }
      />
    </Routes>
  );
}

export default function App() {
  return (
    <I18nProvider>
      <ToastProvider>
        <AuthProvider>
          <AppErrorBoundary>
            <HashRouter>
              <AppRoutes />
            </HashRouter>
          </AppErrorBoundary>
        </AuthProvider>
      </ToastProvider>
    </I18nProvider>
  );
}
