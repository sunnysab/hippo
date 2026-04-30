import { type ReactNode, useState, useEffect, useCallback, useRef } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { TopBar } from './TopBar';
import { apiGet } from '../api';
import { useI18n } from '../i18n';
import { useToast } from '../hooks/useToast';
import { formatRelativeTime } from '../utils/format';

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

  const currentTab = location.pathname.replace(/^\//, '') || 'groups';

  const refreshChromeMeta = useCallback(async () => {
    try {
      const loginPayload = await apiGet('/api/login');
      const lastLogin = loginPayload.last_login as Record<string, unknown> | null;
      if (lastLogin?.updated_at) {
        const ts = formatRelativeTime(lastLogin.updated_at as string);
        setLastLoginAt(ts ? t('login.lastLoginAt', 'Last login {time}').replace('{time}', ts) : '');
      }
      const loginStatus = loginPayload.status as string;
      if (loginStatus === 'login_required' || loginStatus === 'failed') {
        if (loginStatus === 'login_required') {
          setBannerText(t('sync.loginRequired', 'Login required. Please re-login.'));
          setBannerVisible(true);
        } else {
          setBannerText(t('sync.failed', 'Sync failed. Please check login.'));
          setBannerVisible(true);
        }
      } else {
        setBannerVisible(false);
      }

      const syncPayload = await apiGet('/api/settings/status');
      const finished = syncPayload.last_finished_at as string | null;
      if (finished) {
        const ts = formatRelativeTime(finished);
        setLastSyncAt(ts ? t('sync.lastSyncAt', 'Last sync {time}').replace('{time}', ts) : '');
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

  const handleRefresh = async () => {
    await refreshChromeMeta();
    window.dispatchEvent(new CustomEvent('hippo:refresh'));
  };

  const handleBannerLogin = () => {
    navigate('/settings');
  };

  // Listen for toast events from the API layer
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as string;
      showToast(detail);
    };
    window.addEventListener('hippo:toast', handler);
    return () => window.removeEventListener('hippo:toast', handler);
  }, [showToast]);

  return (
    <div className="app" ref={appRef}>
      <TopBar
        topbarRef={topbarRef}
        currentTab={currentTab}
        lastLoginAt={lastLoginAt}
        lastSyncAt={lastSyncAt}
        onRefresh={handleRefresh}
      />
      <main className="content">
        <div className={`banner${bannerVisible ? '' : ' is-hidden'}`} id="status-banner">
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
