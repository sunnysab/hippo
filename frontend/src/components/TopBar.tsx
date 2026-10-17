import type { RefObject } from 'react';
import { NavLink } from 'react-router-dom';
import { useI18n } from '../i18n';
import { useAuth } from '../hooks/useAuth';
import { useTheme } from '../hooks/useTheme';

interface TopBarProps {
  topbarRef: RefObject<HTMLElement | null>;
  currentTab: string;
  syncStatus: SyncStatus;
  lastSyncAt: string;
}

export type SyncStatus = '' | 'online' | 'offline' | 'unreachable';

const SYNC_STATUS_LABEL: Record<Exclude<SyncStatus, ''>, { key: string; fallback: string }> = {
  online: { key: 'sync.status.online', fallback: '同步服务运行中' },
  offline: { key: 'sync.status.offline', fallback: '同步服务未登录' },
  unreachable: { key: 'sync.status.unreachable', fallback: '同步服务不可达' },
};

export function TopBar({ topbarRef, currentTab, syncStatus, lastSyncAt }: TopBarProps) {
  const { t } = useI18n();
  const { user, signOut, isAdmin } = useAuth();
  const { mode, cycle } = useTheme();
  const themeLabel = {
    system: t('theme.system', '跟随系统'),
    light: t('theme.light', '浅色'),
    dark: t('theme.dark', '深色'),
  }[mode];

  const syncLabel = syncStatus
    ? t(SYNC_STATUS_LABEL[syncStatus].key, SYNC_STATUS_LABEL[syncStatus].fallback)
    : '';

  const tabs = [
    { key: 'groups', label: t('nav.groups', 'Groups'), path: '/groups' },
    { key: 'articles', label: t('nav.articles', 'Articles'), path: '/articles' },
    { key: 'knowledge', label: t('nav.knowledge', 'Knowledge'), path: '/knowledge' },
    { key: 'chat', label: t('nav.chat', '对话'), path: '/chat' },
    { key: 'report', label: t('nav.report', '日报'), path: '/report' },
    { key: 'settings', label: t('nav.sync', 'Settings'), path: '/settings/filter' },
    ...(isAdmin ? [{ key: 'admin', label: t('nav.admin', '管理'), path: '/admin' }] : []),
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
        {syncStatus ? (
          <div
            className={`top-meta top-sync-status is-${syncStatus}`}
            id="daemon-status"
            title={lastSyncAt ? `${syncLabel} · ${lastSyncAt}` : syncLabel}
          >
            <span className="status-dot" aria-hidden="true" />
            {syncLabel}
          </div>
        ) : null}
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
