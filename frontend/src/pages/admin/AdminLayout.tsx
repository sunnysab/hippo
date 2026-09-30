import { NavLink, Outlet } from 'react-router-dom';
import { useI18n } from '../../i18n';

export function AdminLayout() {
  const { t } = useI18n();
  const sections = [
    { key: 'user', label: t('admin.nav.user', '用户'), path: '/admin' },
    { key: 'llm', label: t('admin.nav.llm', 'LLM 配置'), path: '/admin/llm' },
    { key: 'site', label: t('admin.nav.site', '站点设置'), path: '/admin/site' },
    { key: 'log', label: t('admin.nav.log', '日志'), path: '/admin/log' },
  ];

  return (
    <div className="admin-layout">
      <nav className="admin-nav" aria-label={t('admin.navAria', '管理面板导航')}>
        {sections.map((section) => (
          <NavLink
            key={section.key}
            end={section.path === '/admin'}
            className={({ isActive }) => `admin-nav-item${isActive ? ' is-active' : ''}`}
            to={section.path}
          >
            {section.label}
          </NavLink>
        ))}
      </nav>
      <div className="admin-main">
        <Outlet />
      </div>
    </div>
  );
}
