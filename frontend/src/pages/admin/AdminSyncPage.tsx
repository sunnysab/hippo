import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSettingsState } from '../../store/settings';
import { useAuth } from '../../hooks/useAuth';
import { apiGet } from '../../api';
import type { SyncSettings, SyncStatus, SyncTask } from '../../store/settings';
import { buildSyncSettingsFormState, type SyncSettingsFormState } from '../settings/form';
import { onRefresh } from '../../utils/events';
import { ActiveTaskPanel } from './ActiveTaskPanel';
import { FailureAlertPanel } from './FailureAlertPanel';
import { QueuePanel } from './QueuePanel';
import { SyncHistoryPanel } from './SyncHistoryPanel';
import { SyncSettingsPanel } from './SyncSettingsPanel';

const buildSyncStatusFingerprint = (payload: Record<string, unknown> | null): string => {
  if (!payload) return '';
  const history = Array.isArray(payload.history) ? payload.history : [];
  const compact = history.map((item: Record<string, unknown>) => ({
    started_at: item?.started_at || '',
    finished_at: item?.finished_at || '',
    status: item?.status || '',
  }));
  return JSON.stringify({
    status: payload.status || '',
    history: compact,
    queue: payload.queue || {},
    worker_heartbeat_at: payload.worker_heartbeat_at || '',
  });
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

export function AdminSyncPage() {
  const { state, dispatch } = useSettingsState();
  const { isAdmin } = useAuth();
  const lastSyncFingerprint = useRef('');
  const lastTasksFingerprint = useRef('');
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
    if (!isAdmin) return;
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
  }, [dispatch, isAdmin]);

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
    };
  }, [getSyncPollDelay, isAdmin, loadSyncStatus, loadSyncTasks]);

  useEffect(() => {
    const handler = () => {
      void loadSyncTasks();
      void loadSyncStatus();
      void loadSyncSettings();
    };
    return onRefresh(handler);
  }, [loadSyncTasks, loadSyncStatus, loadSyncSettings]);

  const initialFormState = useMemo(
    () => buildSyncSettingsFormState(state.syncSettings),
    [state.syncSettings],
  );
  const formResetKey = useMemo(
    () => JSON.stringify(initialFormState),
    [initialFormState],
  );

  return <AdminSyncPanels key={formResetKey} initialFormState={initialFormState} />;
}

function AdminSyncPanels({ initialFormState }: { initialFormState: SyncSettingsFormState }) {
  const [formState, setFormState] = useState(initialFormState);

  return (
    <div className="settings-panel-grid settings-sync-grid">
      <SyncSettingsPanel formState={formState} setFormState={setFormState} />
      <FailureAlertPanel formState={formState} setFormState={setFormState} />
      <ActiveTaskPanel />
      <QueuePanel />
      <SyncHistoryPanel />
    </div>
  );
}
