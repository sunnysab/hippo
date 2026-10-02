import { useEffect, useCallback, useMemo, useState } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import { useSettingsState } from '../../store/settings';
import { useAuth } from '../../hooks/useAuth';
import { apiGet } from '../../api';
import type { SyncSettings } from '../../store/settings';
import {
  buildSyncSettingsFormState,
  type SyncSettingsFormState,
} from './form';
import { useI18n } from '../../i18n';
import type { SettingsRouteContextValue } from './settingsRouteContext';
import { onRefresh } from '../../utils/events';

export function SettingsPage() {
  const { state, dispatch } = useSettingsState();
  const { isAdmin } = useAuth();

  const loadSyncSettings = useCallback(async () => {
    if (!isAdmin) return;
    try {
      const payload = await apiGet('/api/settings');
      dispatch({ type: 'SET_SYNC_SETTINGS', payload: payload as unknown as SyncSettings });
    } catch {
      /* ignore */
    }
  }, [dispatch, isAdmin]);

  useEffect(() => {
    void loadSyncSettings();
  }, [loadSyncSettings]);

  useEffect(() => {
    const handler = () => {
      void loadSyncSettings();
    };
    return onRefresh(handler);
  }, [loadSyncSettings]);

  const initialFormState = useMemo(
    () => buildSyncSettingsFormState(state.syncSettings),
    [state.syncSettings],
  );
  const formResetKey = useMemo(
    () => JSON.stringify(initialFormState),
    [initialFormState],
  );

  return (
    <SettingsPageContent
      key={formResetKey}
      initialFormState={initialFormState}
    />
  );
}

interface SettingsPageContentProps {
  initialFormState: SyncSettingsFormState;
}

function SettingsPageContent({ initialFormState }: SettingsPageContentProps) {
  const { t } = useI18n();
  const { isAdmin } = useAuth();
  const [formState, setFormState] = useState<SyncSettingsFormState>(initialFormState);

  const navItems = [
    {
      key: 'filter',
      path: '/settings/filter',
      title: t('settings.navFilter', 'Filter'),
      summary: t('settings.navFilterSummary', 'Manage article filters and reading preferences.'),
    },
    ...(isAdmin
      ? [
          {
            key: 'email',
            path: '/settings/email',
            title: t('settings.navEmail', 'Email'),
            summary: t('settings.navEmailSummary', 'Configure SMTP and test delivery.'),
          },
        ]
      : []),
  ];

  const outletContext: SettingsRouteContextValue = {
    formState,
    setFormState,
  };

  return (
    <section id="view-settings" className="view is-active">
      <div className="rail-layout">
        <aside className="panel rail-sidebar">
          <div className="panel-header rail-sidebar-header">
            <div>
              <h2>{t('settings.title', 'Settings')}</h2>
              <p className="muted">{t('settings.subtitle', 'Manage filters and email settings.')}</p>
            </div>
          </div>
          <nav className="rail-nav" aria-label={t('settings.navAria', 'Settings sections')}>
            <div className="rail-nav-list">
              {navItems.map((item) => (
                <NavLink
                  key={item.key}
                  to={item.path}
                  className={({ isActive: itemActive }) => `rail-nav-item${itemActive ? ' is-active' : ''}`}
                >
                  <span className="rail-nav-label">{item.title}</span>
                  <span className="rail-nav-summary">{item.summary}</span>
                </NavLink>
              ))}
            </div>
          </nav>
        </aside>
        <div className="rail-main">
          <Outlet context={outletContext} />
        </div>
      </div>
    </section>
  );
}
