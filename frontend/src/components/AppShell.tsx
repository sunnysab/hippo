import { type ReactNode, useState, useEffect, useCallback, useRef } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { TopBar, type SyncStatus } from './TopBar';
import { apiGet } from '../api';
import { useI18n } from '../i18n';
import { useAuth } from '../hooks/useAuth';
import { useToast } from '../hooks/useToast';
import { formatRelativeTime } from '../utils/format';
import { onToast } from '../utils/events';

export function AppShell({ children }: { children: ReactNode }) {
  const location = useLocation();
  const navigate = useNavigate();
  const { t } = useI18n();
  const { isAdmin } = useAuth();
  const { showToast } = useToast();
  const appRef = useRef<HTMLDivElement>(null);
  const topbarRef = useRef<HTMLElement>(null);
  const [syncStatus, setSyncStatus] = useState<SyncStatus>('');
  const [lastSyncAt, setLastSyncAt] = useState('');
  const [bannerVisible, setBannerVisible] = useState(false);
  const [bannerText, setBannerText] = useState('');
  const [bannerError, setBannerError] = useState(false);
  // Only a signed-out or expired WeChat daemon can be renewed from the QR page.
  const [bannerRelogin, setBannerRelogin] = useState(false);

  const currentTab = location.pathname.split('/').filter(Boolean)[0] || 'groups';

  const refreshChromeMeta = useCallback(async () => {
    // The WeChat daemon state and the sync status are independent: a failed
    // status call must not throw away a login state we already have, and a
    // stale sync error must not hide a login that needs renewing.
    let daemonHealthy = false;
    try {
      const loginPayload = await apiGet('/api/login');
      const loggedIn = Boolean(loginPayload.logged_in);
      const loginStatus = String(loginPayload.status || '');
      daemonHealthy = loggedIn;
      if (loggedIn) {
        setSyncStatus('online');
        // A recovered WeChat login withdraws its own error and relogin button
        // right away, without depending on the sync status call succeeding.
        setBannerText('');
        setBannerVisible(false);
        setBannerError(false);
        setBannerRelogin(false);
      } else if (loginStatus === 'unreachable') {
        setSyncStatus('unreachable');
      } else {
        setSyncStatus('offline');
      }

      if (!loggedIn) {
        const detail = String(loginPayload.error || '').trim();
        const unreachable = loginStatus === 'unreachable';
        const expired = loginStatus === 'expired' || Boolean(loginPayload.need_relogin);
        const label = unreachable
          ? t('banner.weixinUnreachable', 'WeChat service is unreachable')
          : expired
            ? t('banner.weixinLoginExpired', 'WeChat login has expired. Please sign in again.')
            : t('banner.weixinNotLoggedIn', 'WeChat account is not signed in. Please sign in again.');
        setBannerText(detail ? `${label} (${detail})` : label);
        setBannerVisible(true);
        setBannerError(unreachable);
        // A dead daemon process cannot be fixed by scanning a QR code.
        setBannerRelogin(!unreachable);
      }
    } catch {
      /* a failed login probe leaves the banner as it is */
    }

    try {
      const syncPayload = await apiGet('/api/settings/status');
      // The push channel stores articles continuously while the list job may
      // not have run for days, so report whichever happened most recently.
      const latest = [syncPayload.last_ingest_at, syncPayload.last_finished_at]
        .filter((value): value is string => typeof value === 'string' && value !== '')
        .sort((a, b) => new Date(b).getTime() - new Date(a).getTime())[0];
      if (latest) {
        const ts = formatRelativeTime(latest, t);
        setLastSyncAt(ts ? t('sync.lastSyncAt', 'Last sync {time}').replace('{time}', ts) : '');
      } else {
        setLastSyncAt('');
      }

      // The daemon state wins: only a healthy login can be blamed on a sync
      // error, which may come from any account.
      if (daemonHealthy) {
        const lastError = syncPayload.last_error as string | null;
        if (lastError) {
          setBannerText(t('banner.syncFailed', 'Sync failed: {message}').replace('{message}', lastError));
          setBannerVisible(true);
          setBannerError(true);
          setBannerRelogin(false);
        } else {
          setBannerVisible(false);
          setBannerError(false);
          setBannerRelogin(false);
        }
      }
    } catch {
      /* the sync status is optional; the login banner stays as it is */
    }
  }, [t]);

  useEffect(() => {
    const kickoff = window.setTimeout(() => {
      void refreshChromeMeta();
    }, 0);
    const timer = window.setInterval(() => {
      void refreshChromeMeta();
    }, 60000);
    return () => {
      window.clearTimeout(kickoff);
      window.clearInterval(timer);
    };
  }, [refreshChromeMeta]);

  useEffect(() => {
    const app = appRef.current;
    const topbar = topbarRef.current;
    if (!app || !topbar) return;

    const updateTopbarHeight = () => {
      app.style.setProperty('--app-topbar-height', `${Math.ceil(topbar.getBoundingClientRect().height)}px`);
    };

    updateTopbarHeight();
    const observer = new ResizeObserver(updateTopbarHeight);
    observer.observe(topbar);
    window.addEventListener('resize', updateTopbarHeight);

    return () => {
      observer.disconnect();
      window.removeEventListener('resize', updateTopbarHeight);
    };
  }, []);

  const handleBannerLogin = () => {
    // 扫码重登是公众号这个来源的能力，所以落在它的来源页上。
    navigate('/admin/source/weixin');
  };

  const handleBannerDismiss = () => {
    setBannerVisible(false);
    setBannerText('');
  };

  // Listen for toast events from the API layer
  useEffect(() => {
    return onToast(showToast);
  }, [showToast]);

  return (
    <div className="app" ref={appRef}>
      <TopBar
        topbarRef={topbarRef}
        currentTab={currentTab}
        syncStatus={syncStatus}
        lastSyncAt={lastSyncAt}
      />
      <main className="content">
        <div className={`banner${bannerVisible ? '' : ' is-hidden'}${bannerError ? ' is-error' : ''}`} id="status-banner">
          <div className="banner-text" id="banner-text">{bannerText}</div>
          <div className="banner-actions">
            {isAdmin && bannerRelogin ? (
              <button
                className="btn ghost"
                id="btn-banner-login"
                type="button"
                onClick={handleBannerLogin}
              >
                {t('login.relogin', 'Re-login')}
              </button>
            ) : null}
            <button
              className="btn ghost"
              id="btn-banner-dismiss"
              type="button"
              aria-label={t('common.close', 'Close')}
              onClick={handleBannerDismiss}
            >
              ×
            </button>
          </div>
        </div>
        <div id="view-root">{children}</div>
      </main>
    </div>
  );
}
