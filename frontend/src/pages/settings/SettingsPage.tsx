import { useEffect, useCallback, useMemo, useRef, useState } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import { useSettingsState } from '../../store/settings';
import { apiGet, apiSend } from '../../api';
import type { SyncStatus, SyncSettings, SyncTask, LoginStatus } from '../../store/settings';
import {
  buildSyncSettingsFormState,
  type SyncSettingsFormState,
} from './form';
import { useI18n } from '../../i18n';
import type { SettingsRouteContextValue } from './settingsRouteContext';
import { onRefresh } from '../../utils/events';

const buildSyncStatusFingerprint = (payload: Record<string, unknown> | null): string => {
  if (!payload) return '';
  const history = Array.isArray(payload.history) ? payload.history : [];
  const compact = history.map((item: Record<string, unknown>) => ({
    started_at: item?.started_at || '',
    finished_at: item?.finished_at || '',
    status: item?.status || '',
  }));
  return JSON.stringify({ status: payload.status || '', history: compact });
};

const buildSyncTasksFingerprint = (tasks: SyncTask[]): string => {
  return JSON.stringify(tasks.map((t) => ({
    task_id: t.task_id,
    status: t.status,
    started_at: t.started_at,
    finished_at: t.finished_at,
    accounts_done: t.accounts_done,
    accounts_total: t.accounts_total,
  })));
};

export function SettingsPage() {
  const { state, dispatch } = useSettingsState();
  const lastSyncFingerprint = useRef('');
  const lastTasksFingerprint = useRef('');
  const loginPollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const hasActiveTask = useCallback(() => {
    const tasks = state.syncTasks || [];
    if (tasks.some((task) => task.status === 'running' || task.status === 'pending')) return true;
    return state.syncStatus?.status === 'running' || state.syncStatus?.status === 'pending';
  }, [state.syncStatus?.status, state.syncTasks]);

  const getSyncPollDelay = useCallback(() => (hasActiveTask() ? 1000 : 10000), [hasActiveTask]);

  const loadSyncStatus = useCallback(async () => {
    try {
      const payload = await apiGet('/api/settings/status');
      const fingerprint = buildSyncStatusFingerprint(payload);
      if (fingerprint !== lastSyncFingerprint.current) {
        lastSyncFingerprint.current = fingerprint;
        dispatch({ type: 'SET_SYNC_STATUS', payload: payload as unknown as SyncStatus });
      }
    } catch {
      /* ignore */
    }
  }, [dispatch]);

  const loadSyncTasks = useCallback(async () => {
    try {
      const payload = await apiGet('/api/settings/tasks?limit=5&detail=true');
      const tasks = (payload.tasks || []) as SyncTask[];
      const fingerprint = buildSyncTasksFingerprint(tasks);
      if (fingerprint !== lastTasksFingerprint.current) {
        lastTasksFingerprint.current = fingerprint;
        dispatch({ type: 'SET_SYNC_TASKS', tasks });
      }
    } catch {
      /* ignore */
    }
  }, [dispatch]);

  const loadSyncSettings = useCallback(async () => {
    try {
      const payload = await apiGet('/api/settings');
      dispatch({ type: 'SET_SYNC_SETTINGS', payload: payload as unknown as SyncSettings });
    } catch {
      /* ignore */
    }
  }, [dispatch]);

  const scheduleLoginPoll = useCallback((status: string) => {
    if (loginPollTimer.current) {
      clearInterval(loginPollTimer.current);
      loginPollTimer.current = null;
    }
    if (['waiting', 'scanned', 'refresh', 'starting', 'confirmed'].includes(status)) {
      loginPollTimer.current = setInterval(async () => {
        try {
          const payload = await apiSend('/api/login/poll', 'POST', {});
          dispatch({ type: 'SET_LOGIN_STATUS', payload: payload as unknown as LoginStatus });
          const nextStatus = (payload.status as string) || 'idle';
          if (nextStatus === 'confirmed') {
            // Stop poll BEFORE calling finalize to prevent concurrent finalize calls
            if (loginPollTimer.current) clearInterval(loginPollTimer.current);
            loginPollTimer.current = null;
            try {
              const result = await apiSend('/api/login/finalize', 'POST', {});
              dispatch({ type: 'SET_LOGIN_STATUS', payload: result as unknown as LoginStatus });
            } catch {
              // Re-fetch server state so UI reflects the actual status
              try {
                const fresh = await apiGet('/api/login');
                dispatch({ type: 'SET_LOGIN_STATUS', payload: fresh as unknown as LoginStatus });
              } catch {
                dispatch({
                  type: 'SET_LOGIN_STATUS',
                  payload: { status: 'error', message: 'Login finalization failed', updated_at: new Date().toISOString() } as LoginStatus,
                });
              }
            }
          } else if (['success', 'error', 'idle'].includes(nextStatus)) {
            if (loginPollTimer.current) clearInterval(loginPollTimer.current);
            loginPollTimer.current = null;
          }
        } catch {
          /* ignore */
        }
      }, 2000);
    }
  }, [dispatch]);

  const loadLoginStatus = useCallback(async () => {
    try {
      const payload = await apiGet('/api/login');
      dispatch({ type: 'SET_LOGIN_STATUS', payload: payload as unknown as LoginStatus });
      scheduleLoginPoll((payload.status as string) || 'idle');
    } catch {
      /* ignore */
    }
  }, [dispatch, scheduleLoginPoll]);

  useEffect(() => {
    void loadSyncSettings();
    void loadLoginStatus();
  }, [loadSyncSettings, loadLoginStatus]);

  useEffect(() => {
    let syncTimer: ReturnType<typeof setTimeout> | null = null;

    const clearSyncTimer = () => {
      if (syncTimer) {
        clearTimeout(syncTimer);
        syncTimer = null;
      }
    };

    const scheduleNextPoll = (delay: number) => {
      clearSyncTimer();
      syncTimer = setTimeout(() => {
        void (async () => {
          await loadSyncTasks();
          await loadSyncStatus();
          scheduleNextPoll(getSyncPollDelay());
        })();
      }, Math.max(delay, 500));
    };

    void loadSyncTasks();
    void loadSyncStatus();
    scheduleNextPoll(getSyncPollDelay());

    return () => {
      clearSyncTimer();
      if (loginPollTimer.current) clearInterval(loginPollTimer.current);
    };
  }, [getSyncPollDelay, loadSyncStatus, loadSyncTasks]);

  useEffect(() => {
    const handler = () => {
      void loadSyncTasks();
      void loadSyncStatus();
      void loadSyncSettings();
      void loadLoginStatus();
    };
    return onRefresh(handler);
  }, [loadSyncTasks, loadSyncStatus, loadSyncSettings, loadLoginStatus]);

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
  const [formState, setFormState] = useState<SyncSettingsFormState>(initialFormState);

  const navItems = [
    {
      key: 'sync',
      path: '/settings/sync',
      title: t('settings.navSync', 'Sync'),
      summary: t('settings.navSyncSummary', 'Manage schedules, progress, and alerts.'),
    },
    {
      key: 'filter',
      path: '/settings/filter',
      title: t('settings.navFilter', 'Filter'),
      summary: t('settings.navFilterSummary', 'Manage article filters and reading preferences.'),
    },
    {
      key: 'email',
      path: '/settings/email',
      title: t('settings.navEmail', 'Email'),
      summary: t('settings.navEmailSummary', 'Configure SMTP and test delivery.'),
    },
    {
      key: 'login',
      path: '/settings/login',
      title: t('settings.navLogin', 'Login'),
      summary: t('settings.navLoginSummary', 'Check sign-in state and refresh the session.'),
    },
  ];

  const outletContext: SettingsRouteContextValue = {
    formState,
    setFormState,
  };

  return (
    <section id="view-settings" className="view is-active">
      <div className="settings-layout">
        <aside className="panel settings-sidebar">
          <div className="panel-header settings-sidebar-header">
            <div>
              <h2>{t('settings.title', 'Settings')}</h2>
              <p className="muted">{t('settings.subtitle', 'Manage login, sync, filters, and email settings.')}</p>
            </div>
          </div>
          <nav className="settings-nav" aria-label={t('settings.navAria', 'Settings sections')}>
            <div className="settings-nav-list">
              {navItems.map((item) => (
                <NavLink
                  key={item.key}
                  to={item.path}
                  className={({ isActive: itemActive }) => `settings-nav-item${itemActive ? ' is-active' : ''}`}
                >
                  <span className="settings-nav-label">{item.title}</span>
                  <span className="settings-nav-summary">{item.summary}</span>
                </NavLink>
              ))}
            </div>
          </nav>
        </aside>
        <div className="settings-main">
          <Outlet context={outletContext} />
        </div>
      </div>
    </section>
  );
}
