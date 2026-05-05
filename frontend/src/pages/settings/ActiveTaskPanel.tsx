import { useEffect, useState } from 'react';
import { useSettingsState } from '../../store/settings';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { apiSend, isAuthError } from '../../api';
import { escapeHtml, formatRelativeTime } from '../../utils/format';
import { getSyncTone } from '../../utils/sync';
import { emitRefresh } from '../../utils/events';

export function ActiveTaskPanel() {
  const { state } = useSettingsState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const [now, setNow] = useState(() => Date.now());
  const [cancelling, setCancelling] = useState(false);
  const [cancellingTaskId, setCancellingTaskId] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setNow(Date.now());
    }, 60000);
    return () => window.clearInterval(timer);
  }, []);

  const getActiveTask = () => {
    const tasks = state.syncTasks || [];
    return tasks.find((t) => t.status === 'running') || tasks.find((t) => t.status === 'pending') || null;
  };

  const activeTask = getActiveTask();

  const handleCancel = async () => {
    if (!activeTask) return;
    setCancelling(true);
    setCancellingTaskId(activeTask.task_id);
    try {
      await apiSend(`/api/settings/tasks/${activeTask.task_id}/cancel`, 'POST', {});
      showToast(t('sync.cancelled', '同步已取消'));
      emitRefresh();
    } catch (err) {
      if (isAuthError(err)) return;
      showToast((err as Error)?.message || t('sync.cancelFailed', '取消失败'));
      setCancelling(false);
      setCancellingTaskId(null);
    }
  };

  const isCancelling = cancelling && activeTask?.task_id === cancellingTaskId;
  const canCancel = !isCancelling && activeTask && (activeTask.status === 'running' || activeTask.status === 'pending');

  const phaseLabel = (phase: string | undefined | null): string => {
    if (phase === 'listing') return t('sync.phase.listing', '抓列表');
    if (phase === 'content') return t('sync.phase.content', '下正文');
    if (phase === 'images') return t('sync.phase.images', '下图片');
    return '';
  };

  return (
    <div className="panel sync-active">
      <div className="panel-header">
        <div>
          <h2>{t('sync.activeTitle', 'Active Task')}</h2>
          <p className="muted">{t('sync.activeSubtitle', 'Live progress for the current sync task.')}</p>
        </div>
        {canCancel ? (
          <div className="toolbar">
            <button className="btn ghost" type="button" onClick={handleCancel} disabled={isCancelling}>
              {isCancelling ? t('sync.cancelling', '取消中…') : t('sync.cancel', '取消')}
            </button>
          </div>
        ) : null}
      </div>
      <div className="sync-active-body" id="sync-active-task">
        {!activeTask ? (
          <div className="empty-state">{t('sync.activeEmpty', 'No active sync task.')}</div>
        ) : (
          <div>
            <div className="sync-active-summary">
              <div className="sync-summary-copy">
                <div className="sync-summary-top">
                  <div className="account-name">
                    {escapeHtml(
                      (activeTask.status === 'pending'
                        ? t('sync.pendingTitle', '排队中 {name}')
                        : t('sync.runningTitle', '正在同步 {name}')
                      ).replace('{name}', activeTask.current_account?.nickname || activeTask.current_account?.biz || t('sync.runningProgress', '处理中'))
                    )}
                  </div>
                  <span className={`sync-status-badge sync-tone-${getSyncTone(activeTask.status)}`}>
                    {t(`sync.status.${activeTask.status || 'running'}`, '同步中')}
                  </span>
                  {activeTask.phase && (
                    <span className="sync-phase-badge">{phaseLabel(activeTask.phase)}</span>
                  )}
                </div>
                <div className="account-sub">
                  {activeTask.started_at || activeTask.created_at
                    ? t('sync.runningSince', 'Started {n} minutes ago').replace(
                        '{n}', String(Math.max(1, Math.floor((now - new Date(activeTask.started_at || activeTask.created_at).getTime()) / 60000))),
                      )
                    : ''}
                </div>
              </div>
              <div className="sync-summary-side">
                {activeTask.accounts_total > 0 && (
                  <span className="meta-count">{activeTask.accounts_done || 0}/{activeTask.accounts_total}</span>
                )}
              </div>
            </div>
            {activeTask.accounts_total > 0 && (
              <div className="sync-progress-track" aria-hidden="true">
                <span style={{ width: `${Math.max(4, Math.round(((activeTask.accounts_done || 0) / activeTask.accounts_total) * 100))}%` }}></span>
              </div>
            )}
            <div className="sync-progress-list">
              {(activeTask.accounts || []).slice(0, 8).map((account) => {
                const toneClass = `sync-tone-${getSyncTone(account.status)}`;
                const articleBadge = account.article_total !== null && account.article_total !== undefined
                  ? `${account.article_current || 0}/${account.article_total}`
                  : account.article_current ? `${account.article_current}` : '';
                return (
                  <div key={account.biz} className={`sync-progress-item ${account.status === 'running' ? 'is-running' : ''} ${toneClass}`}>
                    <div className="sync-progress-copy">
                      <div className="sync-progress-title-row">
                        <div className="account-name">{escapeHtml(account.nickname || account.biz || '-')}</div>
                        <span className={`sync-progress-status sync-status-badge ${toneClass}`}>
                          {t(`sync.status.${account.status}`, account.status || '')}
                        </span>
                      </div>
                      <div className="account-sub">
                        {[phaseLabel(account.phase), formatRelativeTime(account.updated_at, t)].filter(Boolean).join(' · ')}
                      </div>
                    </div>
                    {articleBadge ? <span className="meta-count">{escapeHtml(articleBadge)}</span> : null}
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
