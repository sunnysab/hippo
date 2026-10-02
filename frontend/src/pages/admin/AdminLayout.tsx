import { NavLink, Outlet } from 'react-router-dom';
import { useI18n } from '../../i18n';

export function AdminLayout() {
  const { t } = useI18n();
  const sections = [
    { key: 'user', label: t('admin.nav.user', '用户'), path: '/admin' },
    { key: 'sync', label: t('admin.nav.sync', '同步'), path: '/admin/sync' },
    { key: 'login', label: t('admin.nav.login', '登录'), path: '/admin/login' },
    { key: 'llm', label: t('admin.nav.llm', 'LLM 配置'), path: '/admin/llm' },
    { key: 'site', label: t('admin.nav.site', '站点设置'), path: '/admin/site' },
    { key: 'log', label: t('admin.nav.log', '日志'), path: '/admin/log' },
  ];

  return (
    <section id="view-admin" className="view is-active">
      <div className="rail-layout">
        <aside className="panel rail-sidebar">
          <div className="panel-header rail-sidebar-header">
            <div>
              <h2>{t('admin.title', 'Admin')}</h2>
              <p className="muted">{t('admin.subtitle', 'Manage users, sync, sign-in and site settings.')}</p>
            </div>
          </div>
          <nav className="rail-nav" aria-label={t('admin.navAria', '管理面板导航')}>
            <div className="rail-nav-list">
              {sections.map((section) => (
                <NavLink
                  key={section.key}
                  end={section.path === '/admin'}
                  className={({ isActive }) => `rail-nav-item${isActive ? ' is-active' : ''}`}
                  to={section.path}
                >
                  <span className="rail-nav-label">{section.label}</span>
                </NavLink>
              ))}
            </div>
          </nav>
        </aside>
        <div className="rail-main">
          <Outlet />
        </div>
      </div>
    </section>
  );
}
