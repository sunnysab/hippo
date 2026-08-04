import { useState } from 'react';
import { useSettingsState, type LoginStatus } from '../../store/settings';
import { useI18n } from '../../i18n';
import { apiSend, isAuthError } from '../../api';
import { emitRefresh } from '../../utils/events';
import { formatRelativeTime } from '../../utils/format';
import { getSyncTone } from '../../utils/sync';

interface ImportForm {
  vid: string;
  access_token: string;
  refresh_token: string;
  device_id: string;
}

const EMPTY_FORM: ImportForm = { vid: '', access_token: '', refresh_token: '', device_id: '' };

export function LoginPanel() {
  const { state, dispatch } = useSettingsState();
  const { t } = useI18n();

  const loginStatus = state.loginStatus;
  const status = loginStatus?.status || 'missing';
  const message = loginStatus?.message || loginStatus?.last_error || '';
  const updatedAt = loginStatus?.updated_at || '';
  const hasCredential = !!loginStatus?.has_credential;
  const vid = loginStatus?.vid || '';
  const nickname = loginStatus?.nickname || '';

  const [form, setForm] = useState<ImportForm>(EMPTY_FORM);
  const [busy, setBusy] = useState(false);

  const send = async (path: string, body: Record<string, unknown>) => {
    setBusy(true);
    try {
      const payload = await apiSend(path, 'POST', body);
      dispatch({ type: 'SET_LOGIN_STATUS', payload: payload as unknown as LoginStatus });
      emitRefresh();
    } catch (err) {
      if (isAuthError(err)) return;
    } finally {
      setBusy(false);
    }
  };

  const importCredential = () => {
    if (!form.vid.trim() || !form.access_token.trim()) return;
    void send('/api/login/import', { ...form });
  };

  const refresh = () => {
    void send('/api/login/refresh', {});
  };

  const clear = () => {
    void send('/api/login/clear', {});
  };

  const statusLabel = () => {
    const key = `login.status.${status}`;
    return t(key, message || status);
  };

  const metaText = hasCredential
    ? `${nickname || vid}${updatedAt ? ' · ' + formatRelativeTime(updatedAt, t) : ''}`
    : t('login.missing', 'No credential imported.');

  return (
    <div className="panel sync-login">
      <div className="panel-header">
        <div>
          <h2>{t('login.title', 'Login')}</h2>
          <p className="muted">{t('login.subtitle', 'Import your WeRead credentials to enable sync.')}</p>
        </div>
        <div className="toolbar">
          <button className="btn" id="btn-login-refresh" type="button" onClick={refresh} disabled={!hasCredential || busy}>
            {t('login.refresh', 'Refresh Token')}
          </button>
          <button
            className="btn ghost"
            id="btn-login-clear"
            type="button"
            onClick={clear}
            disabled={!hasCredential || busy}
          >
            {t('login.clear', 'Clear')}
          </button>
        </div>
      </div>
      <div className="login-card" data-status={status}>
        <div className={`login-status sync-status-badge sync-tone-${getSyncTone(status)}`} id="login-status">
          {statusLabel()}
        </div>
        <div className="login-meta" id="login-meta">
          {metaText}
        </div>
      </div>
      <div className="login-import">
        <p className="muted">{t('login.importHint', 'Paste WeRead credentials exported from the Android app.')}</p>
        <label>
          <span>vid</span>
          <input value={form.vid} onChange={(e) => setForm({ ...form, vid: e.target.value })} autoComplete="off" />
        </label>
        <label>
          <span>accessToken</span>
          <input value={form.access_token} onChange={(e) => setForm({ ...form, access_token: e.target.value })} autoComplete="off" />
        </label>
        <label>
          <span>refreshToken</span>
          <input value={form.refresh_token} onChange={(e) => setForm({ ...form, refresh_token: e.target.value })} autoComplete="off" />
        </label>
        <label>
          <span>deviceId</span>
          <input value={form.device_id} onChange={(e) => setForm({ ...form, device_id: e.target.value })} autoComplete="off" />
        </label>
        <button
          className="btn"
          id="btn-login-import"
          type="button"
          onClick={importCredential}
          disabled={busy || !form.vid.trim() || !form.access_token.trim()}
        >
          {t('login.import', 'Import')}
        </button>
      </div>
    </div>
  );
}
