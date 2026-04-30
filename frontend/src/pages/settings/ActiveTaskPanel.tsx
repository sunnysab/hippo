import { useSettingsState } from '../../store/settings';
import { useI18n } from '../../i18n';
import { escapeHtml, formatRelativeTime } from '../../utils/format';

export function ActiveTaskPanel() {
  const { state } = useSettingsState();
  const { t } = useI18n();

  const getActiveTask = () => {
    const tasks = state.syncTasks || [];
    return tasks.find((t) => t.status === 'running') || tasks.find((t) => t.status === 'pending') || null;
  };

  const getSyncTone = (status: string) => {
    if (status === 'success' || status === 'completed') return 'success';
    if (status === 'failed' || status === 'login_required' || status === 'stopped') return 'danger';
    if (status === 'running') return 'info';
    if (status === 'pending') return 'warning';
    if (status === 'skipped') return 'muted';
    return 'neutral';
  };

  const activeTask = getActiveTask();

  return (
    <div className="panel sync-active">
      <div className="panel-header">
        <div>
          <div className="section-kicker">{t('sync.activeKicker', 'Live Run')}</div>
          <h2>{t('sync.activeTitle', 'Active Task')}</h2>
          <p className="muted">{t('sync.activeSubtitle', 'Live progress for the current sync task.')}</p>
        </div>
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
                        ? t('sync.pendingTitle', 'Queued {name}')
                        : t('sync.runningTitle', 'Syncing {name}')
                      ).replace('{name}', activeTask.current_account?.nickname || activeTask.current_account?.biz || t('sync.runningProgress', 'Processing'))
                    )}
                  </div>
                  <span className={`sync-status-badge sync-tone-${getSyncTone(activeTask.status)}`}>
                    {t(`sync.status.${activeTask.status || 'running'}`, activeTask.status || 'running')}
                  </span>
                </div>
                <div className="account-sub">
                  {activeTask.started_at || activeTask.created_at
                    ? t('sync.runningSince', 'Started {n} minutes ago').replace(
                        '{n}', String(Math.max(1, Math.floor((Date.now() - new Date(activeTask.started_at || activeTask.created_at).getTime()) / 60000))),
                      )
                    : ''}
                </div>
              </div>
              <div className="sync-summary-side">
                {activeTask.accounts_total > 0 && (
                  <span className="badge">{activeTask.accounts_done || 0}/{activeTask.accounts_total}</span>
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
                          {t(`sync.status.${account.status}`, account.status)}
                        </span>
                      </div>
                      <div className="account-sub">
                        {formatRelativeTime(account.updated_at)}
                      </div>
                    </div>
                    {articleBadge ? <span className="badge">{escapeHtml(articleBadge)}</span> : null}
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
