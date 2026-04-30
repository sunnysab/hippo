import { useEffect, useCallback, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { useSettingsState } from '../../store/settings';
import { useI18n } from '../../i18n';
import { apiGet, apiSend } from '../../api';
import { LoginPanel } from './LoginPanel';
import { SyncSettingsPanel } from './SyncSettingsPanel';
import { EmailPanel } from './EmailPanel';
import { ActiveTaskPanel } from './ActiveTaskPanel';
import { SyncHistoryPanel } from './SyncHistoryPanel';
import type { SyncStatus, SyncSettings, SyncTask, LoginStatus } from '../../store/settings';
import {
  buildSyncSettingsFormState,
  type SyncSettingsFormState,
} from './form';

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
    task_id: t.task_id, status: t.status, started_at: t.started_at, finished_at: t.finished_at,
    accounts_done: t.accounts_done, accounts_total: t.accounts_total,
  })));
};

export function SettingsPage() {
  const location = useLocation();
  const isActive = location.pathname === '/settings';
  const { state, dispatch } = useSettingsState();
  const { t } = useI18n();
  const lastSyncFingerprint = useRef('');
  const lastTasksFingerprint = useRef('');
  const syncPollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const loginPollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const [formState, setFormState] = useState<SyncSettingsFormState>(
    () => buildSyncSettingsFormState(state.syncSettings),
  );

  const hasActiveTask = () => {
    const tasks = state.syncTasks || [];
    if (tasks.some((t) => t.status === 'running' || t.status === 'pending')) return true;
    return state.syncStatus?.status === 'running' || state.syncStatus?.status === 'pending';
  };

  const getSyncPollDelay = () => hasActiveTask() ? 1000 : 10000;

  const loadSyncStatus = useCallback(async () => {
    try {
      const payload = await apiGet('/api/settings/status');
      const fp = buildSyncStatusFingerprint(payload);
      if (fp !== lastSyncFingerprint.current) {
        lastSyncFingerprint.current = fp;
        dispatch({ type: 'SET_SYNC_STATUS', payload: payload as unknown as SyncStatus });
      }
    } catch { /* ignore */ }
  }, [dispatch]);

  const loadSyncTasks = useCallback(async () => {
    try {
      const payload = await apiGet('/api/settings/tasks?limit=5&detail=true');
      const tasks = (payload.tasks || []) as SyncTask[];
      const fp = buildSyncTasksFingerprint(tasks);
      if (fp !== lastTasksFingerprint.current) {
        lastTasksFingerprint.current = fp;
        dispatch({ type: 'SET_SYNC_TASKS', tasks });
      }
    } catch { /* ignore */ }
  }, [dispatch]);

  const loadSyncSettings = useCallback(async () => {
    try {
      const payload = await apiGet('/api/settings');
      dispatch({ type: 'SET_SYNC_SETTINGS', payload: payload as unknown as SyncSettings });
    } catch { /* ignore */ }
  }, [dispatch]);

  const loadLoginStatus = useCallback(async () => {
    try {
      const payload = await apiGet('/api/login');
      dispatch({ type: 'SET_LOGIN_STATUS', payload: payload as unknown as LoginStatus });
      scheduleLoginPoll((payload.status as string) || 'idle');
    } catch { /* ignore */ }
  }, [dispatch]);

  const scheduleLoginPoll = (status: string) => {
    if (loginPollTimer.current) {
      clearInterval(loginPollTimer.current);
      loginPollTimer.current = null;
    }
    if (['waiting', 'scanned', 'refresh', 'starting'].includes(status)) {
      loginPollTimer.current = setInterval(async () => {
        try {
          const payload = await apiSend('/api/login/poll', 'POST', {});
          dispatch({ type: 'SET_LOGIN_STATUS', payload: payload as unknown as LoginStatus });
          const newStatus = (payload.status as string) || 'idle';
          if (['success', 'error', 'idle'].includes(newStatus)) {
            if (loginPollTimer.current) clearInterval(loginPollTimer.current);
            loginPollTimer.current = null;
          }
        } catch { /* ignore */ }
      }, 2000);
    }
  };

  const scheduleSyncPoll = (delay: number) => {
    if (syncPollTimer.current) clearTimeout(syncPollTimer.current);
    syncPollTimer.current = setTimeout(async () => {
      if (!isActive) return;
      await loadSyncTasks();
      await loadSyncStatus();
      scheduleSyncPoll(getSyncPollDelay());
    }, Math.max(delay, 500));
  };

  // Init
  useEffect(() => {
    void loadSyncSettings();
    void loadLoginStatus();
  }, [loadSyncSettings, loadLoginStatus]);

  useEffect(() => {
    setFormState(buildSyncSettingsFormState(state.syncSettings));
  }, [state.syncSettings]);

  // Route enter/leave
  useEffect(() => {
    if (isActive) {
      void loadSyncTasks();
      void loadSyncStatus();
      scheduleSyncPoll(getSyncPollDelay());
    } else {
      if (syncPollTimer.current) clearTimeout(syncPollTimer.current);
      if (loginPollTimer.current) clearInterval(loginPollTimer.current);
    }
    return () => {
      if (syncPollTimer.current) clearTimeout(syncPollTimer.current);
      if (loginPollTimer.current) clearInterval(loginPollTimer.current);
    };
  }, [isActive]);

  // Refresh handler
  useEffect(() => {
    const handler = () => {
      void loadSyncTasks();
      void loadSyncStatus();
      void loadSyncSettings();
      void loadLoginStatus();
    };
    window.addEventListener('hippo:refresh', handler);
    return () => window.removeEventListener('hippo:refresh', handler);
  }, [loadSyncTasks, loadSyncStatus, loadSyncSettings, loadLoginStatus]);

  return (
    <section id="view-settings" className="view is-active">
      <div className="sync-page-shell">
        <div className="panel-grid">
          <LoginPanel />
          <SyncSettingsPanel formState={formState} setFormState={setFormState} />
          <EmailPanel formState={formState} setFormState={setFormState} />
          <ActiveTaskPanel />
          <SyncHistoryPanel />
        </div>
      </div>
    </section>
  );
}
