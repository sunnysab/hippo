import type { RefObject } from 'react';
import { NavLink } from 'react-router-dom';
import { useI18n } from '../i18n';

interface TopBarProps {
  topbarRef: RefObject<HTMLElement | null>;
  currentTab: string;
  lastLoginAt: string;
  lastSyncAt: string;
}

export function TopBar({ topbarRef, currentTab, lastLoginAt, lastSyncAt }: TopBarProps) {
  const { t } = useI18n();

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
        {lastLoginAt && <div className="top-meta" id="last-login-info">{lastLoginAt}</div>}
        {lastSyncAt && <div className="top-meta" id="last-sync-info">{lastSyncAt}</div>}
      </div>
    </header>
  );
}
