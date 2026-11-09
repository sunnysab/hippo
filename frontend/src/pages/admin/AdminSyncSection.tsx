import { useCallback, useState } from 'react';
import { apiGet } from '../../api';
import { useI18n } from '../../i18n';
import { useSyncRun } from '../../hooks/useSyncRun';
import { formatRelativeTime } from '../../utils/format';
import { getSyncTaskKind, getSyncRunLabel, getSyncTone } from '../../utils/sync';
import { usePolling } from '../../hooks/usePolling';
import type { SyncTask } from '../../store/settings';

const TASK_LIMIT = 8;

/**
 * One source's sync: what it is doing now and what it did last.
 *
 * Tasks are read from the job queue rather than the in-memory status: only jobs
 * survive a restart, and `source` is a column on them, so "this source's last
 * runs" is a query and not a guess.
 */
export function AdminSyncSection({ source }: { source?: string }) {
  const { t } = useI18n();
  const { runSync } = useSyncRun();
  const [tasks, setTasks] = useState<SyncTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  const [error, setError] = useState('');

  const load = useCallback(async (signal: AbortSignal) => {
    try {
      const query = new URLSearchParams({ limit: String(TASK_LIMIT), detail: 'true', active: 'false' });
      if (source) query.set('source', source);
      const history = await apiGet(`/api/settings/tasks?${query.toString()}`, { signal });
      let active: SyncTask[] = [];
      if (source) {
        query.set('active', 'true');
        query.set('limit', '100');
        const payload = await apiGet(`/api/settings/tasks?${query.toString()}`, { signal });
        active = (payload.tasks || []) as SyncTask[];
      }
      if (signal.aborted) return;
      setTasks([...active, ...((history.tasks || []) as SyncTask[])]);
      setError('');
    } catch (err) {
      if (!signal.aborted) setError((err as Error).message);
    } finally {
      if (!signal.aborted) setLoading(false);
    }
  }, [source]);
  usePolling(load, 5000);

  const run = async () => {
    setBusy(true);
    try {
      // The console runs the instance's own catalogue; the hook reports the job.
      await runSync({ source, siteWide: true });
      await load(new AbortController().signal);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <h2>{t('sync.historyTitle', '同步记录')}</h2>
          <p className="muted">
            {source
              ? t('admin.sync.sourceSubtitle', '这个来源最近的任务。')
              : t('sync.historySubtitle', '最近的同步状态。')}
          </p>
        </div>
        <div className="toolbar">
          <button className="btn" type="button" disabled={busy} onClick={() => void run()}>
            {t('sync.run', '运行')}
          </button>
        </div>
      </div>

      {error && <p role="alert">{error}</p>}
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
            const saved = Number(task.report?.total_saved ?? task.accounts?.reduce((sum, account) => sum + (account.saved || 0), 0) ?? 0);
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
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
