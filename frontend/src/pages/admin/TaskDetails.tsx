import { useCallback, useState } from 'react';
import { apiGet } from '../../api';
import { usePolling } from '../../hooks/usePolling';
import { useI18n } from '../../i18n';
import type { SyncTask } from '../../store/settings';
import { getSyncTone } from '../../utils/sync';

export function TaskDetails({ taskId, active }: { taskId: string; active: boolean }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [task, setTask] = useState<SyncTask | null>(null);
  const [error, setError] = useState('');
  const load = useCallback(async (signal: AbortSignal) => {
    try {
      const payload = await apiGet(`/api/settings/tasks/${taskId}`, { signal });
      if (signal.aborted) return;
      setTask(payload as unknown as SyncTask);
      setError('');
    } catch (err) {
      if (!signal.aborted) setError((err as Error).message);
    }
  }, [taskId]);
  usePolling(load, open ? active ? 5000 : 60000 : null);

  return <details onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t('sync.taskDetails', '账号明细')}</summary>
    {open && <>
      {error && <p role="alert">{error}</p>}
      {!task && !error && <p className="muted">{t('admin.loading', '加载中…')}</p>}
      {task?.accounts?.map((account) => <div className="sync-progress-item" key={account.biz}>
        <div className="sync-progress-copy">
          <div className="sync-progress-title-row">
            <span>{account.nickname || account.biz}</span>
            <span className={`sync-status-badge sync-tone-${getSyncTone(account.status)}`}>
              {t(`sync.status.${account.status}`, account.status)}
            </span>
          </div>
          <div className="account-sub">
            {t('sync.enqueuedCount', '新入队 {n} 篇').replace('{n}', String(account.saved || 0))}
            {` · ${account.page_count || 0} 页`}
            {account.error && ` · ${account.error}`}
          </div>
        </div>
      </div>)}
    </>}
  </details>;
}
