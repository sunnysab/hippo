import { useSettingsState, type LoginStatus } from '../../store/settings';
import { useI18n } from '../../i18n';
import { apiSend, isAuthError } from '../../api';
import { emitRefresh } from '../../utils/events';
import { formatRelativeTime } from '../../utils/format';
import { getSyncTone } from '../../utils/sync';

export function LoginPanel() {
  const { state, dispatch } = useSettingsState();
  const { t } = useI18n();

  const loginStatus = state.loginStatus;
  const status = loginStatus?.status || 'idle';
  const message = loginStatus?.message || '';
  const updatedAt = loginStatus?.updated_at || '';
  const qrcodeUrl = loginStatus?.qrcode_url;
  const lastLogin = loginStatus?.last_login;
  const qrCodeSrc = qrcodeUrl
    ? `${qrcodeUrl}${qrcodeUrl.includes('?') ? '&' : '?'}v=${encodeURIComponent(updatedAt || `${status}:${message}`)}`
    : '';

  const isInProgress = ['starting', 'waiting', 'scanned', 'refresh'].includes(status);

  const startLogin = async () => {
    const currentStatus = loginStatus?.status || 'idle';
    const force = ['starting', 'waiting', 'scanned', 'refresh'].includes(currentStatus);
    try {
      const payload = await apiSend('/api/login/start', 'POST', { force });
      dispatch({ type: 'SET_LOGIN_STATUS', payload: payload as unknown as LoginStatus });
      emitRefresh();
    } catch (err) {
      if (isAuthError(err)) return;
    }
  };

  const cancelLogin = async () => {
    try {
      const payload = await apiSend('/api/login/cancel', 'POST', {});
      dispatch({ type: 'SET_LOGIN_STATUS', payload: payload as unknown as LoginStatus });
      emitRefresh();
    } catch (err) {
      if (isAuthError(err)) return;
    }
  };

  const getStatusLabel = () => {
    const statusKey = `login.status.${status}`;
    return t(statusKey, message || '');
  };

  return (
    <div className="panel sync-login">
      <div className="panel-header">
        <div>
          <h2>{t('login.title', 'Login')}</h2>
          <p className="muted">{t('login.subtitle', 'Scan the QR code to sign in or refresh the session.')}</p>
        </div>
        <div className="toolbar">
          <button className="btn" id="btn-login-start" type="button" onClick={startLogin}>
            {isInProgress ? t('login.refresh', 'Refresh Login') : t('login.start', 'Start Login')}
          </button>
          <button
            className={`btn ghost${isInProgress ? '' : ' is-hidden'}`}
            id="btn-login-cancel"
            type="button"
            onClick={cancelLogin}
          >
            {t('login.cancel', 'Cancel')}
          </button>
        </div>
      </div>
      <div className="login-card" data-status={status}>
        <div className={`login-status sync-status-badge sync-tone-${getSyncTone(status)}`} id="login-status">
          {getStatusLabel()}
        </div>
        <img
          id="login-qr"
          className={`login-qr${qrcodeUrl ? '' : ' is-hidden'}`}
          alt="QR"
          src={qrCodeSrc || undefined}
        />
        <div className="login-meta" id="login-meta">
          {lastLogin?.nickname ? `${t('login.lastLogin', 'Last login')}: ${lastLogin.nickname} · ${formatRelativeTime(lastLogin.updated_at as string, t)}` : ''}
        </div>
      </div>
    </div>
  );
}
