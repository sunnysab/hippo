import { useEffect, useCallback, useMemo, useRef, useState, type Dispatch, type SetStateAction } from 'react';
import { useLocation } from 'react-router-dom';
import { useSettingsState } from '../../store/settings';
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

interface SettingsPanelsProps {
  initialFormState: SyncSettingsFormState;
}

function SettingsPanels({ initialFormState }: SettingsPanelsProps) {
  const [formState, setFormState] = useState<SyncSettingsFormState>(initialFormState);

  return (
    <>
      <LoginPanel />
      <SyncSettingsPanel formState={formState} setFormState={setFormState as Dispatch<SetStateAction<SyncSettingsFormState>>} />
      <EmailPanel formState={formState} setFormState={setFormState as Dispatch<SetStateAction<SyncSettingsFormState>>} />
      <ActiveTaskPanel />
      <SyncHistoryPanel />
    </>
  );
}

export function SettingsPage() {
  const location = useLocation();
  const isActive = location.pathname === '/settings';
  const { state, dispatch } = useSettingsState();
  const lastSyncFingerprint = useRef('');
  const lastTasksFingerprint = useRef('');
  const loginPollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const hasActiveTask = useCallback(() => {
    const tasks = state.syncTasks || [];
    if (tasks.some((t) => t.status === 'running' || t.status === 'pending')) return true;
    return state.syncStatus?.status === 'running' || state.syncStatus?.status === 'pending';
  }, [state.syncStatus?.status, state.syncTasks]);

  const getSyncPollDelay = useCallback(() => (hasActiveTask() ? 1000 : 10000), [hasActiveTask]);

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

  const scheduleLoginPoll = useCallback((status: string) => {
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
  }, [dispatch]);

  const loadLoginStatus = useCallback(async () => {
    try {
      const payload = await apiGet('/api/login');
      dispatch({ type: 'SET_LOGIN_STATUS', payload: payload as unknown as LoginStatus });
      scheduleLoginPoll((payload.status as string) || 'idle');
    } catch { /* ignore */ }
  }, [dispatch, scheduleLoginPoll]);

  // Init
  useEffect(() => {
    void loadSyncSettings();
    void loadLoginStatus();
  }, [loadSyncSettings, loadLoginStatus]);

  // Route enter/leave
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
          if (!isActive) return;
          await loadSyncTasks();
          await loadSyncStatus();
          scheduleNextPoll(getSyncPollDelay());
        })();
      }, Math.max(delay, 500));
    };

    if (isActive) {
      void loadSyncTasks();
      void loadSyncStatus();
      scheduleNextPoll(getSyncPollDelay());
    } else {
      if (loginPollTimer.current) clearInterval(loginPollTimer.current);
    }

    return () => {
      clearSyncTimer();
      if (loginPollTimer.current) clearInterval(loginPollTimer.current);
    };
  }, [getSyncPollDelay, isActive, loadSyncStatus, loadSyncTasks]);

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

  const initialFormState = useMemo(
    () => buildSyncSettingsFormState(state.syncSettings),
    [state.syncSettings],
  );
  const formResetKey = useMemo(
    () => JSON.stringify(initialFormState),
    [initialFormState],
  );

  return (
    <section id="view-settings" className="view is-active">
      <div className="sync-page-shell">
        <div className="panel-grid">
          <SettingsPanels key={formResetKey} initialFormState={initialFormState} />
        </div>
      </div>
    </section>
  );
}
