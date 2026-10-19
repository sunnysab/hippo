import type { RefObject } from 'react';
import { NavLink } from 'react-router-dom';
import { useI18n } from '../i18n';
import { useAuth } from '../hooks/useAuth';
import { useTheme } from '../hooks/useTheme';
import { BrandMark } from './BrandMark';

interface TopBarProps {
  topbarRef: RefObject<HTMLElement | null>;
  currentTab: string;
}

export function TopBar({ topbarRef, currentTab }: TopBarProps) {
  const { t } = useI18n();
  const { user, signOut, isAdmin } = useAuth();
  const { mode, cycle } = useTheme();
  const themeLabel = {
    system: t('theme.system', '跟随系统'),
    light: t('theme.light', '浅色'),
    dark: t('theme.dark', '深色'),
  }[mode];

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
        <BrandMark className="brand-mark" />
        <div className="brand-text">
          <div className="brand-title">Hippo</div>
          <div className="brand-subtitle">
            {t('brand.subtitle', 'Personal information and knowledge platform')}
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
