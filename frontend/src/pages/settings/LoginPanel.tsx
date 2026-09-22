import { useState } from 'react';
import { useSettingsState, type LoginStatus } from '../../store/settings';
import { useI18n } from '../../i18n';
import { apiGet, apiSend, isAuthError } from '../../api';
import { emitRefresh } from '../../utils/events';
import { getSyncTone } from '../../utils/sync';

interface QrState {
  png: string;
  url: string;
}

/**
 * 登录面板：登录完全由 weixin-rs daemon 负责，这里只读状态 + 触发扫码 / 免扫重登。
 *
 * daemon 的 `get_status` 有三态：`online` / `logged_out` / `expired`（带 need_relogin）；
 * hippo 自己再加一态 `unreachable`（daemon 进程没起来）。
 */
export function LoginPanel() {
  const { state, dispatch } = useSettingsState();
  const { t } = useI18n();

  const loginStatus = state.loginStatus;
  const status = loginStatus?.status || 'unknown';
  const loggedIn = Boolean(loginStatus?.logged_in);
  const error = loginStatus?.error || '';
  const nickname = loginStatus?.nickname || '';
  const wxid = loginStatus?.wxid || '';
  const clients = loginStatus?.clients_connected;

  const [qr, setQr] = useState<QrState | null>(null);
  const [busy, setBusy] = useState(false);
  const [runError, setRunError] = useState('');

  const refreshStatus = async () => {
    const payload = (await apiGet('/api/login')) as unknown as LoginStatus;
    dispatch({ type: 'SET_LOGIN_STATUS', payload });
  };

  const run = async (task: () => Promise<void>) => {
    setBusy(true);
    setRunError('');
    try {
      await task();
    } catch (err) {
      if (!isAuthError(err)) setRunError(err instanceof Error ? err.message : String(err));
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

  const statusLabel = () => {
    if (status === 'online') return t('login.status.online', 'daemon online');
    if (status === 'logged_out') return t('login.status.loggedOut', 'daemon is not signed in');
    if (status === 'expired') return t('login.status.expired', 'daemon session expired');
    if (status === 'unreachable') return t('login.status.unreachable', 'daemon is unreachable');
    return t('login.status.unknown', status);
  };

  const metaText = loggedIn
    ? [
        nickname || wxid,
        typeof clients === 'number'
          ? t('login.clients', '{n} clients connected').replace('{n}', String(clients))
          : '',
      ]
        .filter(Boolean)
        .join(' · ')
    : t('login.missing', 'daemon is not signed in; scan or use auto re-login.');

  return (
    <div className="panel sync-login">
      <div className="panel-header">
        <div>
          <h2>{t('login.title', 'Login')}</h2>
          <p className="muted">
            {t('login.subtitle', 'The weixin-rs daemon owns the session; check or renew it here.')}
          </p>
        </div>
        <div className="toolbar">
          <button className="btn" id="btn-login-auto" type="button" onClick={autoLogin} disabled={busy}>
            {t('login.auto', 'Auto re-login')}
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
      {(runError || error) && (
        <p className="muted" id="login-error">
          {runError || error}
        </p>
      )}
    </div>
  );
}
