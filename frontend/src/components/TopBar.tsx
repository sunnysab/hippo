import type { RefObject } from 'react';
import { useNavigate } from 'react-router-dom';
import { useI18n } from '../i18n';

interface TopBarProps {
  topbarRef: RefObject<HTMLElement | null>;
  currentTab: string;
  lastLoginAt: string;
  lastSyncAt: string;
  onRefresh: () => void;
}

export function TopBar({ topbarRef, currentTab, lastLoginAt, lastSyncAt, onRefresh }: TopBarProps) {
  const navigate = useNavigate();
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
      <nav className="tabs" role="tablist">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            className={`tab${currentTab === tab.key ? ' is-active' : ''}`}
            data-tab={tab.key}
            onClick={() => navigate(tab.path)}
          >
            {tab.label}
          </button>
        ))}
      </nav>
      <div className="top-actions">
        {lastLoginAt && <div className="top-meta" id="last-login-info">{lastLoginAt}</div>}
        {lastSyncAt && <div className="top-meta" id="last-sync-info">{lastSyncAt}</div>}
        <button className="btn ghost" id="btn-refresh" type="button" onClick={onRefresh}>
          <span className="icon">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M4 12a8 8 0 0 1 13.66-5.66L20 4v6h-6l2.44-2.44A6 6 0 1 0 18 12h2a8 8 0 0 1-16 0z" />
            </svg>
          </span>
          <span>{t('actions.refresh', 'Refresh')}</span>
        </button>
      </div>
    </header>
  );
}
