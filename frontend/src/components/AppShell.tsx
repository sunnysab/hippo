import { type ReactNode, useState, useEffect, useCallback, useRef } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { TopBar } from './TopBar';
import { apiGet } from '../api';
import { useI18n } from '../i18n';
import { useToast } from '../hooks/useToast';
import { formatRelativeTime } from '../utils/format';
import { onToast } from '../utils/events';

export function AppShell({ children }: { children: ReactNode }) {
  const location = useLocation();
  const navigate = useNavigate();
  const { t } = useI18n();
  const { showToast } = useToast();
  const appRef = useRef<HTMLDivElement>(null);
  const topbarRef = useRef<HTMLElement>(null);
  const [lastLoginAt, setLastLoginAt] = useState('');
  const [lastSyncAt, setLastSyncAt] = useState('');
  const [bannerVisible, setBannerVisible] = useState(false);
  const [bannerText, setBannerText] = useState('');
  const [bannerError, setBannerError] = useState(false);

  const currentTab = location.pathname.split('/').filter(Boolean)[0] || 'groups';

  const refreshChromeMeta = useCallback(async () => {
    try {
      const loginPayload = await apiGet('/api/login');
      const lastLogin = loginPayload.last_login as Record<string, unknown> | null;
      if (lastLogin?.updated_at) {
        const ts = formatRelativeTime(lastLogin.updated_at as string, t);
        setLastLoginAt(ts ? t('login.lastLoginAt', 'Last login {time}').replace('{time}', ts) : '');
      } else {
        setLastLoginAt('');
      }

      const syncPayload = await apiGet('/api/settings/status');
      const finished = syncPayload.last_finished_at as string | null;
      if (finished) {
        const ts = formatRelativeTime(finished, t);
        setLastSyncAt(ts ? t('sync.lastSyncAt', 'Last sync {time}').replace('{time}', ts) : '');
      } else {
        setLastSyncAt('');
      }

      const lastError = syncPayload.last_error as string | null;
      if (lastError) {
        setBannerText(lastError);
        setBannerVisible(true);
        setBannerError(true);
      } else {
        const loginStatus = loginPayload.status as string;
        if (loginStatus === 'login_required' || loginStatus === 'failed') {
          if (loginStatus === 'login_required') {
            setBannerText(t('sync.loginRequired', 'Login required. Please re-login.'));
            setBannerVisible(true);
          } else {
            setBannerText(t('sync.failed', 'Sync failed. Please check login.'));
            setBannerVisible(true);
          }
          setBannerError(false);
        } else {
          setBannerVisible(false);
          setBannerError(false);
        }
      }
    } catch {
      /* ignore errors during meta refresh */
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
    navigate('/settings/login');
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
        lastLoginAt={lastLoginAt}
        lastSyncAt={lastSyncAt}
      />
      <main className="content">
        <div className={`banner${bannerVisible ? '' : ' is-hidden'}${bannerError ? ' is-error' : ''}`} id="status-banner">
          <div className="banner-text" id="banner-text">{bannerText}</div>
          <button
            className="btn ghost"
            id="btn-banner-login"
            type="button"
            onClick={handleBannerLogin}
          >
            {t('login.relogin', 'Re-login')}
          </button>
        </div>
        <div id="view-root">{children}</div>
      </main>
    </div>
  );
}
