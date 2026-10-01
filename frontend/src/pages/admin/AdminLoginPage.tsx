import { useEffect } from 'react';
import { useSettingsState, type LoginStatus } from '../../store/settings';
import { apiGet } from '../../api';
import { LoginPanel } from './LoginPanel';

export function AdminLoginPage() {
  const { dispatch } = useSettingsState();

  useEffect(() => {
    void (async () => {
      try {
        const payload = await apiGet('/api/login');
        dispatch({ type: 'SET_LOGIN_STATUS', payload: payload as unknown as LoginStatus });
      } catch {
        /* ignore */
      }
    })();
  }, [dispatch]);

  return (
    <div className="settings-panel-grid settings-single-column">
      <LoginPanel />
    </div>
  );
}
