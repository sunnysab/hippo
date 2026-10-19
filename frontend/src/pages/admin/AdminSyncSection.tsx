import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiSend } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { formatRelativeTime } from '../../utils/format';
import { getSyncTone } from '../../utils/sync';
import type { SyncTask } from '../../store/settings';
import { getSourceLabel } from '../../utils/source';

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
  const { showToast } = useToast();
  const [tasks, setTasks] = useState<SyncTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const query = new URLSearchParams({ limit: String(TASK_LIMIT), detail: 'true' });
      if (source) query.set('source', source);
      const payload = await apiGet(`/api/settings/tasks?${query.toString()}`);
      setTasks((payload.tasks as SyncTask[]) || []);
    } finally {
      setLoading(false);
    }
  }, [source]);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async () => {
    setBusy(true);
    try {
      const body = source ? { source } : {};
      await apiSend('/api/settings/run', 'POST', body);
      showToast(
        source
          ? t('admin.sync.queuedFor', '{source} 的同步已排队').replace('{source}', getSourceLabel(source, t))
          : t('sync.runQueued', '同步任务已进入队列'),
      );
      await load();
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
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
            const saved = Number(task.accounts?.reduce((sum, account) => sum + (account.saved || 0), 0) ?? 0);
            return (
              <div key={task.task_id} className={`list-item sync-history-item ${toneClass}`}>
                <div className="sync-history-copy">
                  <div className="sync-history-top">
                    <div className="account-name">
                      {saved > 0
                        ? t('admin.sync.savedCount', '+{n} 篇').replace('{n}', String(saved))
                        : t(`sync.status.${task.status}`, task.status)}
                    </div>
                    <span className={`sync-status-badge ${toneClass}`}>
                      {t(`sync.status.${task.status}`, task.status)}
                    </span>
                  </div>
                  <div className="account-sub">
                    {task.accounts_total > 0
                      ? `${task.accounts_done || 0}/${task.accounts_total} · `
                      : ''}
                    {rel}
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
