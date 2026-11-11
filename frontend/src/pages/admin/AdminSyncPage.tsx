import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSettingsState } from '../../store/settings';
import { useAuth } from '../../hooks/useAuth';
import { apiGet } from '../../api';
import type { SyncSettings, SyncStatus, SyncTaskSummary } from '../../store/settings';
import { buildSyncSettingsFormState, type SyncSettingsFormState } from '../settings/form';
import { emitRefresh, onRefresh } from '../../utils/events';
import { usePolling } from '../../hooks/usePolling';
import { SyncHealthPanel } from './SyncHealthPanel';
import { ActiveTaskPanel } from './ActiveTaskPanel';
import { FailureAlertPanel } from './FailureAlertPanel';
import { QueuePanel } from './QueuePanel';
import { AdminSyncSection } from './AdminSyncSection';
import { SyncSettingsPanel } from './SyncSettingsPanel';
import { SourceSyncOverview } from './SourceSyncOverview';

/** The persistent task queue and download backlog are independent parts of ingestion. */
export function AdminSyncPage() {
  const { state, dispatch } = useSettingsState();
  const { isAdmin } = useAuth();
  const previousActive = useRef<Set<string>>(new Set());
  const [error, setError] = useState('');
  const [settingsError, setSettingsError] = useState('');
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const loadProgress = useCallback(async (signal: AbortSignal) => {
    if (!isAdmin) return;
    try {
      const [status, jobs] = await Promise.all([
        apiGet('/api/settings/status', { signal }),
        apiGet('/api/settings/tasks?active=true&limit=100&detail=false', { signal }),
      ]);
      if (signal.aborted) return;
      dispatch({ type: 'SET_SYNC_STATUS', payload: status as unknown as SyncStatus });
      const tasks = (jobs.tasks || []) as SyncTaskSummary[];
      dispatch({ type: 'SET_SYNC_TASKS', tasks });
      const ids = new Set(tasks.map((task) => task.task_id));
      const ended = [...previousActive.current].some((id) => !ids.has(id));
      previousActive.current = ids;
      if (ended) emitRefresh();
      setError('');
    } catch (err) {
      if (!signal.aborted) setError((err as Error).message);
    }
  }, [dispatch, isAdmin]);
  const active = state.syncTasks.some((task) => ['running', 'queued', 'pending', 'cancelling'].includes(task.status));
  const refreshProgress = usePolling(loadProgress, active ? 5000 : 10000);

  const loadSyncSettings = useCallback(async () => {
    if (!isAdmin) return;
    try {
      const payload = await apiGet('/api/settings');
      dispatch({ type: 'SET_SYNC_SETTINGS', payload: payload as unknown as SyncSettings });
      setSettingsLoaded(true);
      setSettingsError('');
    } catch (err) {
      setSettingsError((err as Error).message);
    }
  }, [dispatch, isAdmin]);

  useEffect(() => {
    let disposed = false;
    queueMicrotask(() => {
      if (!disposed) void loadSyncSettings();
    });
    return () => { disposed = true; };
  }, [loadSyncSettings]);

  useEffect(() => onRefresh(() => {
    refreshProgress();
    void loadSyncSettings();
  }), [refreshProgress, loadSyncSettings]);

  const initialFormState = useMemo(
    () => buildSyncSettingsFormState(state.syncSettings),
    [state.syncSettings],
  );
  const formResetKey = useMemo(
    () => JSON.stringify(initialFormState),
    [initialFormState],
  );

  return <>
    {error && <p role="alert">{error}</p>}
    {settingsError && <p role="alert">{settingsError}</p>}
    <AdminSyncPanels key={formResetKey} initialFormState={initialFormState} settingsLoaded={settingsLoaded} />
  </>;
}

function AdminSyncPanels({ initialFormState, settingsLoaded }: { initialFormState: SyncSettingsFormState; settingsLoaded: boolean }) {
  const [formState, setFormState] = useState(initialFormState);

  return (
    <div className="admin-page">
      <SyncHealthPanel />
      <ActiveTaskPanel />
      <QueuePanel />
      <SourceSyncOverview />
      {settingsLoaded && <>
        <SyncSettingsPanel formState={formState} setFormState={setFormState} />
        <FailureAlertPanel formState={formState} setFormState={setFormState} />
      </>}
      <AdminSyncSection />
    </div>
  );
}
