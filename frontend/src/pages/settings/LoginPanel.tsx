import { useSettingsState, type LoginStatus } from '../../store/settings';
import { useI18n } from '../../i18n';
import { apiSend } from '../../api';
import { formatRelativeTime } from '../../utils/format';
import { getSyncTone } from '../../utils/sync';

export function LoginPanel() {
  const { state, dispatch } = useSettingsState();
  const { t } = useI18n();

  const loginStatus = state.loginStatus;
  const status = loginStatus?.status || 'idle';
  const message = loginStatus?.message || '';
  const qrcodeUrl = loginStatus?.qrcode_url;
  const lastLogin = loginStatus?.last_login;

  const isInProgress = ['starting', 'waiting', 'scanned', 'refresh'].includes(status);

  const startLogin = async () => {
    const currentStatus = loginStatus?.status || 'idle';
    const force = ['starting', 'waiting', 'scanned', 'refresh'].includes(currentStatus);
    const payload = await apiSend('/api/login/start', 'POST', { force });
    dispatch({ type: 'SET_LOGIN_STATUS', payload: payload as unknown as LoginStatus });
  };

  const cancelLogin = async () => {
    const payload = await apiSend('/api/login/cancel', 'POST', {});
    dispatch({ type: 'SET_LOGIN_STATUS', payload: payload as unknown as LoginStatus });
  };

  const getStatusLabel = () => {
    const statusKey = `login.status.${status}`;
    return t(statusKey, message || '');
  };

  return (
    <div className="panel sync-login">
      <div className="panel-header">
        <div>
          <div className="section-kicker">{t('sync.loginKicker', 'Session')}</div>
          <h2>{t('login.title', 'Login')}</h2>
          <p className="muted">{t('login.subtitle', 'Scan QR code to refresh login session.')}</p>
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
          src={qrcodeUrl ? `${qrcodeUrl}?ts=${Date.now()}` : ''}
        />
        <div className="login-meta" id="login-meta">
          {lastLogin?.nickname ? `${t('login.lastLogin', 'Last login')}: ${lastLogin.nickname} · ${formatRelativeTime(lastLogin.updated_at as string)}` : ''}
        </div>
      </div>
    </div>
  );
}
