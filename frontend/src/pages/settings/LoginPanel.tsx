import { useState } from 'react';
import { useSettingsState, type LoginStatus } from '../../store/settings';
import { useI18n } from '../../i18n';
import { apiGet, apiSend, isAuthError } from '../../api';
import { emitRefresh } from '../../utils/events';
import { formatRelativeTime } from '../../utils/format';
import { getSyncTone } from '../../utils/sync';

interface QrState {
  png: string;
  url: string;
}

/**
 * 登录面板：登录由 weixin-rs daemon 负责（微信读书凭据已废弃）。
 *
 * - 已登录：显示昵称 / wxid；
 * - 未登录：扫码（`/api/login/qr` + `/api/login/wait`）或用本地 auto_auth_key 免扫重登。
 */
export function LoginPanel() {
  const { state, dispatch } = useSettingsState();
  const { t } = useI18n();

  const loginStatus = state.loginStatus;
  const status = loginStatus?.status || 'missing';
  const message = loginStatus?.message || loginStatus?.last_error || '';
  const updatedAt = loginStatus?.updated_at || '';
  const hasCredential = !!loginStatus?.has_credential;
  const nickname = loginStatus?.nickname || '';
  const vid = loginStatus?.vid || '';

  const [qr, setQr] = useState<QrState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const refreshStatus = async () => {
    const payload = (await apiGet('/api/login')) as unknown as LoginStatus;
    dispatch({ type: 'SET_LOGIN_STATUS', payload });
  };

  const run = async (task: () => Promise<void>) => {
    setBusy(true);
    setError('');
    try {
      await task();
    } catch (err) {
      if (!isAuthError(err)) setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const requestQr = () =>
    run(async () => {
      const payload = await apiSend('/api/login/qr', 'POST', {});
      setQr({
        png: String(payload.png_base64 || ''),
        url: String(payload.url || ''),
      });
    });

  const waitLogin = () =>
    run(async () => {
      await apiSend('/api/login/wait', 'POST', {});
      setQr(null);
      await refreshStatus();
      emitRefresh();
    });

  const autoLogin = () =>
    run(async () => {
      await apiSend('/api/login/auto', 'POST', {});
      await refreshStatus();
      emitRefresh();
    });

  const statusLabel = () => t(`login.status.${status}`, message || status);
  const metaText = hasCredential
    ? `${nickname || vid}${updatedAt ? ' · ' + formatRelativeTime(updatedAt, t) : ''}`
    : t('login.missing', 'daemon is not signed in.');

  return (
    <div className="panel sync-login">
      <div className="panel-header">
        <div>
          <h2>{t('login.title', 'Login')}</h2>
          <p className="muted">
            {t('login.subtitle', 'Sign in the weixin-rs daemon that syncs your Official Accounts.')}
          </p>
        </div>
        <div className="toolbar">
          <button className="btn" id="btn-login-auto" type="button" onClick={autoLogin} disabled={busy}>
            {t('login.auto', 'Re-login')}
          </button>
          <button className="btn ghost" id="btn-login-qr" type="button" onClick={requestQr} disabled={busy}>
            {t('login.qr', 'Scan QR')}
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
      {qr && (
        <div className="login-import" id="login-qr-box">
          <p className="muted">{t('login.qrHint', 'Scan with WeChat, then confirm on the phone.')}</p>
          {qr.png ? (
            <img id="login-qr-image" src={`data:image/png;base64,${qr.png}`} alt="login qr" />
          ) : (
            <p id="login-qr-url">{qr.url}</p>
          )}
          <button className="btn" id="btn-login-wait" type="button" onClick={waitLogin} disabled={busy}>
            {t('login.confirm', 'I have confirmed')}
          </button>
        </div>
      )}
      {error && (
        <p className="muted" id="login-error">
          {error}
        </p>
      )}
    </div>
  );
}
