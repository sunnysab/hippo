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
      initialFormState={initialFormState}
      formResetKey={formResetKey}
    />
  );
}

interface SettingsPageContentProps {
  initialFormState: SyncSettingsFormState;
  formResetKey: string;
}

function SettingsPageContent({ initialFormState, formResetKey }: SettingsPageContentProps) {
  const { t } = useI18n();
  const { isAdmin } = useAuth();
  const [formState, setFormState] = useState<SyncSettingsFormState>(initialFormState);
  const [appliedKey, setAppliedKey] = useState(formResetKey);

  // Re-seed the shared sync/email form only when the instance settings really
  // changed. The subtree is not remounted, so a personal filter draft in a
  // sibling panel (and an unchanged email field) survives a same-value refresh.
  if (formResetKey !== appliedKey) {
    setAppliedKey(formResetKey);
    setFormState(initialFormState);
  }

  const navItems = [
    {
      key: 'account',
      path: '/settings/account',
      title: t('settings.navAccount', '账号'),
    },
    {
      key: 'source',
      path: '/settings/source',
      title: t('settings.navSources', '来源凭证'),
    },
    {
      key: 'apikey',
      path: '/settings/apikey',
      title: t('settings.navApiKeys', 'API 密钥'),
    },
    {
      key: 'filter',
      path: '/settings/filter',
      title: t('settings.navFilter', 'Filter'),
    },
    {
      key: 'correction',
      path: '/settings/correction',
      title: t('correction.title', 'Text corrections'),
    },
    ...(isAdmin
      ? [
          {
            key: 'email',
            path: '/settings/email',
            title: t('settings.navEmail', 'Email'),
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
