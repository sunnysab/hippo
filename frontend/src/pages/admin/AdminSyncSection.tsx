import { useCallback, useEffect, useRef, useState } from 'react';
import { apiGet } from '../../api';
import { useI18n } from '../../i18n';
import { useSyncRun } from '../../hooks/useSyncRun';
import { formatRelativeTime } from '../../utils/format';
import { getSyncTaskKind, getSyncRunLabel, getSyncTone } from '../../utils/sync';
import { onRefresh } from '../../utils/events';
import { usePolling } from '../../hooks/usePolling';
import { TaskDetails } from './TaskDetails';
import type { SyncTaskSummary } from '../../store/settings';

const TASK_LIMIT = 8;

/**
 * One source's sync: what it is doing now and what it did last.
 *
 * Tasks are read from the job queue rather than the in-memory status: only jobs
 * survive a restart, and `source` is a column on them, so "this source's last
 * runs" is a query and not a guess.
 */
export function AdminSyncSection({ source }: { source?: string }) {
  return <SourceTaskHistory key={source || 'all'} source={source} />;
}

function SourceTaskHistory({ source }: { source?: string }) {
  const { t } = useI18n();
  const { runSync } = useSyncRun();
  const [historyTasks, setHistoryTasks] = useState<SyncTaskSummary[]>([]);
  const [activeTasks, setActiveTasks] = useState<SyncTaskSummary[]>([]);
  const tasks = [...activeTasks, ...historyTasks.filter((task) => !activeTasks.some((active) => active.task_id === task.task_id))];
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [activeLoaded, setActiveLoaded] = useState(!source);
  const [busy, setBusy] = useState(false);
  const [historyError, setHistoryError] = useState('');
  const [activeError, setActiveError] = useState('');
  const previousActive = useRef<Set<string>>(new Set());

  const loadHistory = useCallback(async (signal: AbortSignal) => {
    const query = new URLSearchParams({ limit: String(TASK_LIMIT), detail: 'false', active: 'false' });
    if (source) query.set('source', source);
    try {
      const payload = await apiGet(`/api/settings/tasks?${query}`, { signal });
      if (signal.aborted) return;
      setHistoryTasks((payload.tasks || []) as SyncTaskSummary[]);
      setHistoryLoaded(true);
      setHistoryError('');
    } catch (err) {
      if (!signal.aborted) { setHistoryError((err as Error).message); setHistoryLoaded(true); }
    }
  }, [source]);
  const refreshHistory = usePolling(loadHistory, 60000);
  const loadActive = useCallback(async (signal: AbortSignal) => {
    if (!source) return;
    try {
      const query = new URLSearchParams({ limit: '100', detail: 'false', active: 'true', source });
      const payload = await apiGet(`/api/settings/tasks?${query}`, { signal });
      if (signal.aborted) return;
      const next = (payload.tasks || []) as SyncTaskSummary[];
      const ids = new Set(next.map((task) => task.task_id));
      if ([...previousActive.current].some((id) => !ids.has(id))) refreshHistory();
      previousActive.current = ids;
      setActiveTasks(next);
      setActiveLoaded(true);
      setActiveError('');
    } catch (err) {
      if (!signal.aborted) { setActiveError((err as Error).message); setActiveLoaded(true); }
    }
  }, [source, refreshHistory]);
  const refreshActive = usePolling(loadActive, source ? 5000 : null);
  const refresh = useCallback(() => { refreshHistory(); refreshActive(); }, [refreshHistory, refreshActive]);
  useEffect(() => onRefresh(refresh), [refresh]);
  const loading = source ? !historyLoaded && !activeLoaded : !historyLoaded;
  const error = historyError || activeError;

  const run = async () => {
    setBusy(true);
    try {
      // The console runs the instance's own catalogue; the hook reports the job.
      await runSync({ source, siteWide: true });
      refresh();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <h2>{t('sync.historyTitle', '同步记录')}</h2>

        </div>
        <div className="toolbar">
          <button className="btn" type="button" disabled={busy} onClick={() => void run()}>
            {t('sync.run', '运行')}
          </button>
        </div>
      </div>

      {error && <p role="alert">{error} <button className="btn ghost" type="button" onClick={refresh}>{t('common.retry', '重试')}</button></p>}
      <div className="list">
        {loading ? (
          <div className="empty-state">{t('admin.loading', '加载中…')}</div>
        ) : tasks.length === 0 ? (
          <div className="empty-state">{t('sync.historyEmpty', '暂无同步记录。')}</div>
        ) : (
          tasks.map((task) => {
            const toneClass = `sync-tone-${getSyncTone(task.status)}`;
            const started = task.started_at || task.created_at;
            const rel = formatRelativeTime(started, t);
            const saved = Number(task.total_saved || 0);
            return (
              <div key={task.task_id} className={`list-item sync-history-item ${toneClass}`}>
                <div className="sync-history-copy">
                  <div className="sync-history-top">
                    <div className="account-name">
                      {getSyncTaskKind(task.trigger_type, t)}
                      {saved > 0 && ` · ${t('sync.enqueuedCount', '新入队 {n} 篇').replace('{n}', String(saved))}`}
                    </div>
                    <span className={`sync-status-badge ${toneClass}`}>
                      {getSyncRunLabel(task.status, t)}
                    </span>
                  </div>
                  <div className="account-sub">
                    {task.accounts_total > 0
                      ? `${task.accounts_done || 0}/${task.accounts_total} · `
                      : ''}
                    {rel}
                    {task.current_account?.nickname ? ` · ${task.current_account.nickname}` : ''}
                    {task.last_log ? ` · ${task.last_log}` : ''}
                    {task.error ? ` · ${task.error}` : ''}
                  </div>
                  <TaskDetails taskId={task.task_id} active={['running', 'queued', 'cancelling'].includes(task.status)} />
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
