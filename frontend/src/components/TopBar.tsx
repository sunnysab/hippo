import type { RefObject } from 'react';
import { NavLink } from 'react-router-dom';
import { useI18n } from '../i18n';
import { useAuth } from '../hooks/useAuth';
import { useTheme } from '../hooks/useTheme';

interface TopBarProps {
  topbarRef: RefObject<HTMLElement | null>;
  currentTab: string;
  daemonStatus: string;
  lastSyncAt: string;
}

export function TopBar({ topbarRef, currentTab, daemonStatus, lastSyncAt }: TopBarProps) {
  const { t } = useI18n();
  const { user, signOut } = useAuth();
  const { mode, cycle } = useTheme();
  const themeLabel = {
    system: t('theme.system', '跟随系统'),
    light: t('theme.light', '浅色'),
    dark: t('theme.dark', '深色'),
  }[mode];

  const tabs = [
    { key: 'groups', label: t('nav.groups', 'Groups'), path: '/groups' },
    { key: 'articles', label: t('nav.articles', 'Articles'), path: '/articles' },
    { key: 'settings', label: t('nav.sync', 'Settings'), path: '/settings/sync' },
  ];

  return (
    <header className="topbar" ref={topbarRef}>
      <div className="brand">
        <span className="brand-mark">H</span>
        <div className="brand-text">
          <div className="brand-title">Hippo</div>
          <div className="brand-subtitle">
            {t('brand.subtitle', 'WeChat Article Studio')}
          </div>
        </div>
      </div>
      <nav className="tabs" aria-label={t('settings.navAria', 'Primary navigation')}>
        {tabs.map((tab) => (
          <NavLink
            key={tab.key}
            className={`tab${currentTab === tab.key ? ' is-active' : ''}`}
            data-tab={tab.key}
            to={tab.path}
          >
            {tab.label}
          </NavLink>
        ))}
      </nav>
      <div className="top-actions">
        {daemonStatus && <div className="top-meta" id="daemon-status">{daemonStatus}</div>}
        {lastSyncAt && <div className="top-meta" id="last-sync-info">{lastSyncAt}</div>}
        {user ? (
          <div className="top-user">
            <button
              className="btn ghost top-theme"
              type="button"
              onClick={cycle}
              title={themeLabel}
              aria-label={themeLabel}
            >
              {mode === 'system' ? '\u25d0' : mode === 'dark' ? '\u263e' : '\u2600'}
            </button>
            <span className="top-user-name" title={user.email ?? user.username}>{user.username}</span>
            <button className="btn ghost" type="button" onClick={() => void signOut()}>
              {t('login.signOut', '退出')}
            </button>
          </div>
        ) : null}
      </div>
    </header>
  );
}
